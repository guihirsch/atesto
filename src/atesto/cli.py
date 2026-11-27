"""Atesto CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import core


def _root(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise core.AtestoError(f"root nao e diretorio: {value}")
    return path


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"numero inteiro positivo esperado: {value!r}"
        ) from exc
    if number < 1:
        raise argparse.ArgumentTypeError(
            f"numero inteiro positivo esperado: {value!r}"
        )
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atesto",
        description="Evidence gate for AI-written code.",
    )
    parser.add_argument(
        "--version", action="version", version=f"atesto {core.VERSION}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="cria atesto.json e o diretorio de artefatos")
    init.add_argument("--root", default=".")
    init.add_argument("--artifacts-root", default=core.DEFAULT_ARTIFACTS_ROOT)
    init.add_argument("--require", default="proof")
    init.add_argument("--test-cmd", default="")
    init.add_argument("--force", action="store_true")

    proof = sub.add_parser("proof", help="executa um comando e grava proof.json")
    proof.add_argument("slug")
    proof.add_argument("--root", default=".")
    proof.add_argument(
        "--no-output",
        action="store_true",
        help="nao captura stdout/stderr no artefato",
    )
    proof.add_argument(
        "exec_command",
        nargs=argparse.REMAINDER,
        help="comando depois de --",
    )

    review = sub.add_parser("review", help="grava review.json no head")
    review.add_argument("slug")
    review.add_argument("--root", default=".")
    review.add_argument("--head", default=None)
    review.add_argument("--verdict", default=None)
    review.add_argument("--reviewer", action="append", default=None)
    review.add_argument("--intent", default=None)
    review.add_argument(
        "--from-json",
        metavar="PATH",
        default=None,
        help="grava o review a partir de um contrato JSON v1",
    )
    review.add_argument(
        "--from-github-pr",
        metavar="N",
        type=_positive_int,
        default=None,
        help="grava o review a partir das aprovacoes do PR N via gh",
    )

    check = sub.add_parser("check", help="valida prova/review no head")
    check.add_argument("--root", default=".")
    check.add_argument("--base")
    check.add_argument("--slug")
    check.add_argument("--ci", action="store_true")
    check.add_argument(
        "--local",
        action="store_true",
        help="compatibilidade; o check ja e local por padrao",
    )
    check.add_argument("--no-test", action="store_true")
    check.add_argument("--json", action="store_true")
    check.add_argument("--quiet", action="store_true")

    doctor = sub.add_parser("doctor", help="diagnostica o repo")
    doctor.add_argument("--root", default=".")

    fingerprint = sub.add_parser("fingerprint", help="imprime o tree hash")
    fingerprint.add_argument("--root", default=".")
    fingerprint.add_argument("--ref")

    hooks = sub.add_parser("hooks", help="gerencia o hook pre-push")
    hooks.add_argument("action", choices=["install", "uninstall", "status"])
    hooks.add_argument("--root", default=".")
    hooks.add_argument("--force", action="store_true")

    return parser


def _run_init(args: argparse.Namespace) -> int:
    root = _root(args.root)
    require = [item.strip() for item in args.require.split(",") if item.strip()]
    path = core.init_config(
        root,
        artifacts_root=args.artifacts_root,
        require=require,
        test_cmd=args.test_cmd,
        force=args.force,
    )
    print(f"atesto: config criada em {path}")
    tracked = core.git(
        root, ["ls-files", "--", args.artifacts_root], check=False
    )
    if tracked.returncode == 0 and tracked.stdout.strip():
        print(
            "atesto: aviso: artifacts_root contem arquivos rastreados; "
            "eles ficarao fora do tree hash"
        )
    print("proximo: atesto proof <slug> -- <comando>")
    return 0


def _run_proof(args: argparse.Namespace) -> int:
    root = _root(args.root)
    command = list(args.exec_command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise core.AtestoError("proof exige comando depois de --")
    exit_code, directory = core.make_proof(
        root,
        args.slug,
        command,
        capture_output=not args.no_output,
    )
    if not args.no_output:
        print(
            "atesto: aviso: proof-output.txt sera commitado; "
            "use --no-output se houver segredo no output"
        )
    print(f"atesto: prova salva em {directory / 'proof.json'} (exit={exit_code})")
    return exit_code


def _run_review(args: argparse.Namespace) -> int:
    root = _root(args.root)
    manual_flags = [
        name
        for name, value in (
            ("--head", args.head),
            ("--verdict", args.verdict),
            ("--reviewer", args.reviewer),
            ("--intent", args.intent),
        )
        if value is not None
    ]
    sources = [
        flag
        for flag, value in (
            ("--from-json", args.from_json),
            ("--from-github-pr", args.from_github_pr),
        )
        if value is not None
    ]
    if len(sources) > 1:
        raise core.AtestoError(
            "review aceita apenas uma fonte: " + ", ".join(sources)
        )
    if sources and manual_flags:
        raise core.AtestoError(
            f"review {sources[0]} nao combina com flags manuais: "
            + ", ".join(manual_flags)
        )
    if args.from_json is not None:
        directory = core.write_review_from_json(
            root, args.slug, Path(args.from_json).expanduser()
        )
    elif args.from_github_pr is not None:
        directory = core.write_review_from_github(
            root, args.slug, args.from_github_pr
        )
    else:
        directory = core.write_review(
            root,
            args.slug,
            head=args.head if args.head is not None else "HEAD",
            verdict=args.verdict if args.verdict is not None else "APPROVED",
            reviewers=args.reviewer or [],
            intent=args.intent if args.intent is not None else "",
        )
    print(f"atesto: review salvo em {directory / 'review.json'}")
    return 0


def _run_check(args: argparse.Namespace) -> int:
    root = _root(args.root)
    result = core.check(
        root,
        base=args.base,
        slug=args.slug,
        ci=args.ci,
        run_test=not args.no_test,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=True, indent=2))
    elif not args.quiet:
        print(
            f"atesto: head={result['head'][:12]} "
            f"artefatos={result['artifacts']} "
            f"tree={result['tree_sha256'][:12]}"
        )
        if result["problems"]:
            for problem in result["problems"]:
                print(f"FAIL  {problem}")
        else:
            print("atesto: ok")
        if result["test_exit"] not in (None, 0):
            print(f"FAIL  test_cmd exit={result['test_exit']}")
    return 0 if result["ok"] else 1


def _run_doctor(args: argparse.Namespace) -> int:
    root = _root(args.root)
    report = core.doctor_report(root)
    head = report["head"]
    if head:
        print(f"git: ok ({head[:12]})")
    else:
        print("git: FAIL")
    suffix = "" if report["config_valid"] else " (INVALIDA)"
    print(f"config: {report['config_name']}{suffix}")
    version = report["config_version"]
    if version is None:
        print("version: desconhecida")
    else:
        print(f"version: {version}")
    if report["unknown_keys"]:
        print(
            "unknown keys (desconhecidas): "
            + ", ".join(report["unknown_keys"])
        )
    print(f"require: {','.join(report['require'])}")
    print(f"min_reviewers: {report['min_reviewers']}")
    print(f"artifacts_root: {report['artifacts_root']}")
    test_cmd = report["test_cmd"]
    print(f"test_cmd: {test_cmd}" if test_cmd else "test_cmd: nenhum")
    print(
        "review_artifacts_paths: "
        + (",".join(report["review_artifacts_paths"]) or "nenhum")
    )
    print(
        "excludes: " + (",".join(report["excludes"]) or "nenhum")
    )
    timeout = report["test_timeout_seconds"]
    print(f"timeout: {timeout}s" if timeout is not None else "timeout: nenhum")
    print(f"hook: {report['hook']}")
    print(f"versao: {report['version']}")
    print(
        f"artefatos: {report['artifacts_state']} "
        f"({report['artifacts_root']})"
    )
    for entry in report["artifacts"]:
        if entry["proof"] == "ausente":
            print(f"artefato {entry['slug']}: proof ausente")
        else:
            print(
                f"artefato {entry['slug']}: proof {entry['proof']} "
                f"(tool={entry['proof_tool']})"
            )
        if entry["review"] == "ausente":
            if entry["review_required"]:
                print(
                    f"artefato {entry['slug']}: review ausente (exigida)"
                )
        else:
            print(
                f"artefato {entry['slug']}: review {entry['review']} "
                f"(tool={entry['review_tool']})"
            )
    if report["issues"]:
        for issue in report["issues"]:
            print(f"FAIL  {issue}")
    else:
        print("atesto: ok")
    return 0 if report["ok"] else 1


def _run_fingerprint(args: argparse.Namespace) -> int:
    root = _root(args.root)
    config = core.load_config(root)
    data = core.tree_fingerprint(root, config, ref=args.ref)
    print(json.dumps(data, ensure_ascii=True, indent=2))
    return 0


def _run_hooks(args: argparse.Namespace) -> int:
    root = _root(args.root)
    if args.action == "install":
        path = core.install_hook(root, force=args.force)
        print(f"atesto: hook instalado em {path}")
        return 0
    if args.action == "uninstall":
        removed = core.uninstall_hook(root)
        print("atesto: hook removido" if removed else "atesto: hook ausente")
        return 0
    print(f"atesto: hook {core.hook_status(root)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return _run_init(args)
        if args.command == "proof":
            return _run_proof(args)
        if args.command == "review":
            return _run_review(args)
        if args.command == "check":
            return _run_check(args)
        if args.command == "doctor":
            return _run_doctor(args)
        if args.command == "fingerprint":
            return _run_fingerprint(args)
        if args.command == "hooks":
            return _run_hooks(args)
        parser.error("comando desconhecido")
        return 2
    except core.GateError as exc:
        if getattr(args, "json", False) and args.command == "check":
            print(
                json.dumps(
                    {"ok": False, "problems": [str(exc)]},
                    ensure_ascii=True,
                    indent=2,
                )
            )
        else:
            print(f"atesto: FAIL: {exc}", file=sys.stderr)
        return 1
    except core.AtestoError as exc:
        if getattr(args, "json", False) and args.command == "check":
            print(
                json.dumps(
                    {"ok": False, "problems": [str(exc)]},
                    ensure_ascii=True,
                    indent=2,
                )
            )
            return 2
        print(f"atesto: erro: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
