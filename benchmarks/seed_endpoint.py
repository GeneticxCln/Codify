"""Point every role at one OpenAI-compatible endpoint, in a state directory of its own.

A real-model baseline is only worth recording if someone else can rebuild its setup. Doing that in
Settings → Agents is eight roles of clicking with no record of what was chosen; this writes it in one
command, and prints the commands that run the benchmark against what it wrote:

    python3 -m benchmarks.seed_endpoint --home /tmp/bench-home \\
        --base-url http://127.0.0.1:8089/v1 --model qwen2.5-1.5b-instruct-q4_k_m

Three rules, each one there because the alternative already went wrong somewhere else in this project:

* **Never the real state directory.** `--home` is where the store and the key go, and `~/.codify` is refused —
  a benchmark has no business editing the roles you actually use.
* **The key goes to the private file, not the database.** `openai_compat` requires a key even for a local
  server (llama.cpp, LM Studio and vLLM ignore it), so without `--api-key-env` a placeholder is stored, and
  the output says it is one.
* **The same destination rule as everywhere else a key is stored** (`docs/03` §1.2): a key is only sent
  over https or to loopback, so a plain-http LAN address is refused here rather than at the first request.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PLACEHOLDER_KEY = "local-endpoint-placeholder"  # noqa: S105 — a documented stand-in, not a credential


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Point every role at one OpenAI-compatible endpoint.")
    parser.add_argument("--home", type=Path, required=True, help="the state directory to write (never ~/.codify)")
    parser.add_argument("--base-url", required=True, help="e.g. http://127.0.0.1:8089/v1")
    parser.add_argument("--model", required=True, help="the model id the endpoint serves")
    parser.add_argument("--provider", default="localserver", help="the provider slug to record (default: localserver)")
    parser.add_argument(
        "--api-key-env", default=None, metavar="VAR",
        help="name of an environment variable holding the key (named, not valued: a key in argv is a key "
             "in the process list); without it a placeholder is stored",
    )
    args = parser.parse_args(argv)

    home = args.home.expanduser().resolve()
    if home == (Path.home() / ".codify").resolve():
        parser.error("refusing the real state directory ~/.codify: a benchmark must not edit the roles you use")

    key = PLACEHOLDER_KEY
    if args.api_key_env:
        key = os.environ.get(args.api_key_env) or ""
        if not key:
            parser.error(f"{args.api_key_env} is not set (or is empty)")

    # Imported here so `--help` and the refusals above cost nothing and touch nothing.
    from engine import home as engine_home
    from engine.db import connect
    from engine.models import ROLES, AgentConfigUpdate
    from engine.providers import Keychain, ProviderError, ProviderFactory
    from engine.services import AgentRegistryService, ApiError

    engine_home.ensure_private_dir(home)
    keychain = Keychain(secrets_path=home / "secrets.json")
    conn = connect(home / "codify.db")
    try:
        registry = AgentRegistryService(conn, ProviderFactory(keychain), keychain)
        try:
            for role in ROLES:
                registry.set_config(role, AgentConfigUpdate(
                    provider=args.provider, protocol="openai_compat", model_name=args.model,
                    base_url=args.base_url, api_key=key,
                ))
        except (ApiError, ProviderError, ValueError) as exc:
            print(f"seed_endpoint: {getattr(exc, 'message', None) or exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()

    db = home / "codify.db"
    print(f"seeded {len(ROLES)} roles -> {args.provider} ({args.base_url}), model {args.model}")
    if key == PLACEHOLDER_KEY:
        print("the key is a placeholder: the server is not checking one, but openai_compat requires it to be set")
    print()
    print("run the benchmark against it (the same three variables keep it out of your real state):")
    print(f"  export CODIFY_HOME={home} CODIFY_DB={db} CODIFY_SECRETS={home / 'secrets.json'}")
    print(f"  python3 -m benchmarks.runner --tier repo_scale --engine-db {db} --repeat 2")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
