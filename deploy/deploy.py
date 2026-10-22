"""Build and deploy the credit bot's Chat callback stack (deploy/template.yaml).

    python deploy/deploy.py --plan        build, upload, create a change set, execute nothing
    python deploy/deploy.py               build, upload, deploy (stack creditbot-<env>)
    python deploy/deploy.py --params deploy/params.prod.json

Needs the AWS CLI (authenticated) and Python. The package is built with Linux wheels
so it runs on the python3.12 Lambda runtime regardless of the build machine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
# One zip serves both functions (callback + worker). boto3 is in the Lambda runtime.
PACKAGE_DEPS = [
    "google-auth>=2.28",
    "google-auth-httplib2>=0.1",
    "google-api-python-client>=2.100",
    "requests>=2.31",
    "python-dotenv>=1.0",
]


def run(*args: str, capture: bool = True) -> str:
    result = subprocess.run(args, capture_output=capture, text=True, encoding="utf-8")
    if result.returncode != 0:
        sys.stderr.write(result.stderr or "")
        raise SystemExit(f"command failed: {' '.join(args)}")
    return (result.stdout or "").strip()


def build_package() -> Path:
    DIST.mkdir(exist_ok=True)
    build = Path(tempfile.mkdtemp(prefix="creditbot-build-"))
    print("[1/3] installing dependencies (manylinux2014_x86_64 / python3.12)")
    run(
        sys.executable, "-m", "pip", "install", "--quiet", "--target", str(build),
        "--platform", "manylinux2014_x86_64", "--python-version", "3.12",
        "--implementation", "cp", "--only-binary=:all:", "--upgrade", "--no-compile",
        *PACKAGE_DEPS,
    )
    print("[2/3] copying credit_bot package and main.py")
    shutil.copytree(ROOT / "credit_bot", build / "credit_bot", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "main.py", build / "main.py")
    for junk in build.rglob("__pycache__"):
        shutil.rmtree(junk, ignore_errors=True)
    out = DIST / "creditbot-callback.zip"
    print(f"[3/3] writing {out}")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(build.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(build).as_posix())
    shutil.rmtree(build, ignore_errors=True)
    print(f"  size {out.stat().st_size / 1e6:.1f} MB")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="prod", choices=["prod", "staging"])
    parser.add_argument("--params", default="")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument(
        "--sa-key", default="",
        help="path of the Gmail-delegated service-account JSON to upload to the state bucket after deploy",
    )
    parser.add_argument(
        "--profile", default="",
        help="AWS CLI profile to deploy with (needs IAM rights, e.g. paybot-admin); default: the environment's",
    )
    parser.add_argument(
        "--chat-key", default="",
        help="path of the Chat app's service-account JSON to upload to the state bucket after deploy",
    )
    parser.add_argument(
        "--tpro-secret", action="store_true",
        help="after deploy, load TPRO_USERNAME / TPRO_PASSWORD from the local .env into the stack's secret",
    )
    args = parser.parse_args()
    if args.profile:
        import os

        os.environ["AWS_PROFILE"] = args.profile

    params_file = Path(args.params) if args.params else ROOT / "deploy" / f"params.{args.env}.json"
    params = json.loads(params_file.read_text(encoding="utf-8"))
    params = {k: v for k, v in params.items() if not k.startswith("_")}

    account = run("aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text")
    stack = f"creditbot-{args.env}"
    code_bucket = params.pop("CodeS3Bucket", "") or f"creditbot-deploy-{account}-{args.region}"
    print(f"account {account} | region {args.region} | stack {stack} | code bucket {code_bucket}")

    zip_path = DIST / "creditbot-callback.zip"
    if not args.skip_build or not zip_path.exists():
        zip_path = build_package()
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()[:16]
    key = f"creditbot-callback-{digest}.zip"

    # code bucket (private, versioned), created once
    exists = subprocess.run(["aws", "s3api", "head-bucket", "--bucket", code_bucket], capture_output=True).returncode == 0
    if not exists:
        print(f"creating code bucket {code_bucket}")
        run("aws", "s3api", "create-bucket", "--bucket", code_bucket, "--region", args.region)
        run("aws", "s3api", "put-public-access-block", "--bucket", code_bucket, "--public-access-block-configuration",
            "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true")
        run("aws", "s3api", "put-bucket-versioning", "--bucket", code_bucket, "--versioning-configuration", "Status=Enabled")
    run("aws", "s3", "cp", str(zip_path), f"s3://{code_bucket}/{key}", "--only-show-errors")
    print(f"uploaded s3://{code_bucket}/{key}")

    overrides = [f"{k}={v}" for k, v in {**params, "StackEnv": args.env, "CodeS3Bucket": code_bucket, "CodeS3Key": key}.items()]
    cmd = [
        "aws", "cloudformation", "deploy", "--region", args.region, "--stack-name", stack,
        "--template-file", str(ROOT / "deploy" / "template.yaml"),
        "--capabilities", "CAPABILITY_IAM", "--no-fail-on-empty-changeset",
        "--parameter-overrides", *overrides,
    ]
    if args.plan:
        cmd.append("--no-execute-changeset")
        print("PLAN: creating a change set without executing it")
    print(run(*cmd))

    if not args.plan:
        outputs = json.loads(run("aws", "cloudformation", "describe-stacks", "--region", args.region, "--stack-name", stack,
                                 "--query", "Stacks[0].Outputs", "--output", "json"))
        print("\nOutputs:")
        for o in outputs:
            print(f"  {o['OutputKey']:18} {o['OutputValue']}")
        bucket = next((o["OutputValue"] for o in outputs if o["OutputKey"] == "StateBucket"), "")
        target = f"s3://{bucket}/config/google-service-account.json"
        if args.sa_key:
            run("aws", "s3", "cp", args.sa_key, target, "--only-show-errors")
            print(f"\nGmail service-account key uploaded to {target}")
        chat_target = f"s3://{bucket}/config/chat-service-account.json"
        if args.chat_key:
            run("aws", "s3", "cp", args.chat_key, chat_target, "--only-show-errors")
            print(f"Chat service-account key uploaded to {chat_target}")
        secret = next((o["OutputValue"] for o in outputs if o["OutputKey"] == "TransportProSecretArn"), "")
        if args.tpro_secret and secret:
            env = {}
            for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
            payload = json.dumps({"username": env.get("TPRO_USERNAME", ""), "password": env.get("TPRO_PASSWORD", "")})
            run("aws", "secretsmanager", "put-secret-value", "--secret-id", secret, "--secret-string", payload)
            print("Transport Pro credentials stored in the stack's secret")
        print(
            "\nOne-time uploads if not done yet:\n"
            f"  aws s3 cp <gmail-key>.json {target}\n"
            f"  aws s3 cp <chat-key>.json {chat_target}\n"
            "  python deploy/deploy.py --skip-build --tpro-secret   (Transport Pro credentials from .env)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
