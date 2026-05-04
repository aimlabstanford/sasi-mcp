"""CLI entry point: ``python -m sasi_mcp <subcommand>`` or ``sasi-mcp <subcommand>``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sasi_mcp.config import Config, load_config
from sasi_mcp.logger import configure_logging, get_logger

_log = get_logger("sasi_mcp.cli")


def _store(cfg: Config):
    from sasi_mcp.store import Store
    return Store(cfg.db_path)


def _cmd_preflight(args: argparse.Namespace, cfg: Config) -> int:
    """Verify the AppleScript path can find the shared mailbox."""
    from sasi_mcp._outlook_bridge import OutlookCacheClient, OutlookCacheError, _run_script

    print(f"mailbox: {cfg.outlook.mailbox_email}")
    try:
        accounts = OutlookCacheClient().list_accounts()
        print(f"cache accounts: {len(accounts)}")
        for rid, name, email in accounts:
            mark = "  *" if email.lower() == cfg.outlook.mailbox_email.lower() else "   "
            print(f"{mark} [{rid}] {name} <{email}>")
    except OutlookCacheError as exc:
        print(f"cache: unavailable ({exc})")

    try:
        sample = _run_script(
            "list_thread_messages.applescript",
            cfg.outlook.mailbox_email, "inbox", "1", "1",
        )
        print(f"applescript: ok, sample={json.dumps(sample)[:200]}")
    except Exception as exc:
        print(f"applescript: FAILED — {exc}")
        return 1
    return 0


def _cmd_ingest(args: argparse.Namespace, cfg: Config) -> int:
    from sasi_mcp.ingest import ingest, parse_since
    since = parse_since(getattr(args, "since", None))
    if not since and getattr(args, "since_last_tick", False):
        with _store(cfg) as store:
            since = parse_since(store.get_meta("last_ingest_at"))
    with _store(cfg) as store:
        counts = ingest(store, cfg.outlook, since=since, limit=args.limit)
    print(json.dumps(counts, indent=2))
    return 0


def _cmd_redact(args: argparse.Namespace, cfg: Config) -> int:
    from sasi_mcp.redact import append_audit, redact_text
    with _store(cfg) as store:
        unredacted = store.list_unredacted()
        if not unredacted:
            print("nothing to redact")
            return 0
        try:
            from sasi_mcp.redact import _build_presidio_engine
            analyzer, anonymizer = _build_presidio_engine()
        except Exception as exc:
            print(f"presidio unavailable ({exc}); using regex pre-pass only", file=sys.stderr)
            analyzer = anonymizer = None
        for m in unredacted:
            result = redact_text(
                m.body_text,
                entities=cfg.redaction.presidio_entities,
                analyzer=analyzer, anonymizer=anonymizer,
            )
            store.update_redacted_body(m.message_id, result.text)
            append_audit(cfg.redaction_audit_path, m.message_id, result.counts)
    print(f"redacted {len(unredacted)} messages")
    return 0


def _cmd_extract(args: argparse.Namespace, cfg: Config) -> int:
    from sasi_mcp.extract import extract_all
    with _store(cfg) as store:
        n = extract_all(store, cfg.llm)
    print(f"extracted {n} pairs")
    return 0


def _cmd_classify(args: argparse.Namespace, cfg: Config) -> int:
    from sasi_mcp.classify import classify_all
    with _store(cfg) as store:
        n = classify_all(store, cfg.llm)
    print(f"classified {n} pairs")
    return 0


def _cmd_review(args: argparse.Namespace, cfg: Config) -> int:
    from sasi_mcp.review import auto_approve, review_interactive
    with _store(cfg) as store:
        if args.auto_approve:
            auto_approve(store, args.confidence)
            return 0
        statuses = ("pending",)
        if args.recheck:
            statuses = ("approved",)
        if args.stale:
            # Re-show pairs that have a stale_reason set.
            from sasi_mcp.store import QAPair
            stale = [p for p in store.list_qa_pairs(include_stale=True)
                     if p.stale_reason and not p.user_overridden_stale]
            from sasi_mcp.review import _render
            for idx, p in enumerate(stale, 1):
                print(_render(p, idx, len(stale)))
                action = (input("[s]upersede  [r]eaffirm  [m]ark_evergreen  [k]eep skipping: ") or "k").strip().lower()[:1]
                if action == "s":
                    new_id = input("new qa_id: ").strip()
                    if new_id:
                        store.supersede(p.qa_id, new_id)
                elif action == "r":
                    store.reaffirm(p.qa_id)
                elif action == "m":
                    store.set_evergreen(p.qa_id, 1.0)
                    store.reaffirm(p.qa_id)
            return 0
        review_interactive(store, reviewer=args.reviewer, statuses=statuses)
    return 0


def _cmd_embed(args: argparse.Namespace, cfg: Config) -> int:
    from sasi_mcp.embed import embed_approved
    with _store(cfg) as store:
        n = embed_approved(store, cfg.embedding.model)
    print(f"embedded {n} pairs")
    return 0


def _cmd_serve(args: argparse.Namespace, cfg: Config) -> int:
    from sasi_mcp.mcp_server import serve
    serve(cfg)
    return 0


def _cmd_stats(args: argparse.Namespace, cfg: Config) -> int:
    with _store(cfg) as store:
        print(json.dumps(store.stats(), indent=2, default=str))
    return 0


def _cmd_audit(args: argparse.Namespace, cfg: Config) -> int:
    from sasi_mcp import audit
    with _store(cfg) as store:
        if args.audit_subcommand == "coverage":
            r = audit.coverage(
                store,
                probe_path=cfg.probe_questions_path,
                similarity_floor=cfg.staleness.similarity_floor,
            )
            print(json.dumps({
                "threads_total": r.threads_total,
                "threads_with_outbound": r.threads_with_outbound,
                "threads_with_qa": r.threads_with_qa,
                "pairs_by_year": r.pairs_by_year,
                "cluster_summaries": r.cluster_summaries,
                "probe_gaps": r.probe_gaps,
            }, indent=2))
        elif args.audit_subcommand == "staleness":
            r = audit.staleness(store, cfg.policy_snapshot_path, cfg.staleness)
            print(json.dumps({
                "year_bound_expired": r.year_bound_expired,
                "snapshot_mismatch": r.snapshot_mismatch,
                "contradicted_by_newer": r.contradicted_by_newer,
            }, indent=2))
        elif args.audit_subcommand == "evergreen":
            scores = audit.evergreen(store, cfg.staleness)
            print(json.dumps(scores, indent=2))
        elif args.audit_subcommand == "queries":
            rows = audit.sample_queries(store, n=args.sample)
            for row in rows:
                print(json.dumps(row, default=str))
        else:
            print("audit: unknown subcommand", file=sys.stderr)
            return 2
    return 0


def _cmd_mark_stale(args: argparse.Namespace, cfg: Config) -> int:
    import re
    pat = re.compile(args.pattern, re.IGNORECASE)
    with _store(cfg) as store:
        n = 0
        for p in store.list_qa_pairs(status="approved"):
            blob = " ".join(filter(None, [p.canonical_question, p.canonical_answer]))
            if pat.search(blob):
                store.mark_stale(p.qa_id, "user_marked")
                n += 1
        print(f"marked {n} pairs stale")
    return 0


def _cmd_supersede(args: argparse.Namespace, cfg: Config) -> int:
    with _store(cfg) as store:
        store.supersede(args.old, args.new)
    print(f"{args.old} -> superseded by {args.new}")
    return 0


def _cmd_reaffirm(args: argparse.Namespace, cfg: Config) -> int:
    with _store(cfg) as store:
        store.reaffirm(args.qa_id)
    print(f"{args.qa_id} reaffirmed")
    return 0


def _cmd_refresh(args: argparse.Namespace, cfg: Config) -> int:
    """Cron-friendly: run ingest --since-last-tick → redact → extract → classify."""
    args.since_last_tick = True
    args.since = None
    rc = _cmd_ingest(args, cfg)
    if rc != 0:
        return rc
    rc = _cmd_redact(args, cfg)
    if rc != 0:
        return rc
    rc = _cmd_extract(args, cfg)
    if rc != 0:
        return rc
    return _cmd_classify(args, cfg)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sasi-mcp")
    p.add_argument("--config", type=Path, default=None,
                   help="path to YAML config (default ~/.sasi-mcp/config.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("preflight").set_defaults(func=_cmd_preflight)

    pi = sub.add_parser("ingest")
    pi.add_argument("--since", type=str, default=None)
    pi.add_argument("--since-last-tick", action="store_true")
    pi.add_argument("--limit", type=int, default=1000)
    pi.set_defaults(func=_cmd_ingest)

    sub.add_parser("redact").set_defaults(func=_cmd_redact)
    sub.add_parser("extract").set_defaults(func=_cmd_extract)
    sub.add_parser("classify").set_defaults(func=_cmd_classify)

    pr = sub.add_parser("review")
    pr.add_argument("--interactive", action="store_true")
    pr.add_argument("--auto-approve", action="store_true")
    pr.add_argument("--confidence", type=float, default=0.85)
    pr.add_argument("--reviewer", type=str, default="local")
    pr.add_argument("--recheck", action="store_true")
    pr.add_argument("--stale", action="store_true")
    pr.set_defaults(func=_cmd_review)

    sub.add_parser("embed").set_defaults(func=_cmd_embed)
    sub.add_parser("serve").set_defaults(func=_cmd_serve)
    sub.add_parser("stats").set_defaults(func=_cmd_stats)

    pa = sub.add_parser("audit")
    pa_sub = pa.add_subparsers(dest="audit_subcommand", required=True)
    pa_sub.add_parser("coverage")
    pa_sub.add_parser("staleness")
    pa_sub.add_parser("evergreen")
    pq = pa_sub.add_parser("queries")
    pq.add_argument("--sample", type=int, default=20)
    pa.set_defaults(func=_cmd_audit)

    pms = sub.add_parser("mark-stale")
    pms.add_argument("--pattern", type=str, required=True)
    pms.set_defaults(func=_cmd_mark_stale)

    psup = sub.add_parser("supersede")
    psup.add_argument("old", type=str)
    psup.add_argument("new", type=str)
    psup.set_defaults(func=_cmd_supersede)

    pra = sub.add_parser("reaffirm")
    pra.add_argument("qa_id", type=str)
    pra.set_defaults(func=_cmd_reaffirm)

    pref = sub.add_parser("refresh")
    pref.add_argument("--limit", type=int, default=1000)
    pref.set_defaults(func=_cmd_refresh)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(cfg.log_dir, level=cfg.log_level)
    return int(args.func(args, cfg))


if __name__ == "__main__":
    sys.exit(main())
