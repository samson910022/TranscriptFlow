#!/usr/bin/env python3
"""
batch_audit.py — Comprehensive batch_status_*.json Auditor

Audit Dimensions:
  A. Schema & Structure — JSON validity, required fields, valid states
  B. Timeliness — last_updated freshness, stuck state detection
  C. Error Visibility — per-chunk error completeness, model attribution
  D. Data Integrity — counts match, no duplicates, no orphans
  E. Model Performance — per-model success/fail/chars_per_sec/error patterns
  F. Cross-File Consistency — against master_file_manifest

Output:
  - Colorized terminal report with per-file audit scores
  - JSON report: {output_dir}/batch_audit_report_{timestamp}.json

Usage:
  python3 batch_audit.py                          # auto-detect output dir
  python3 batch_audit.py --output-dir /path/to/dir
  python3 batch_audit.py --master-file /path/to/manifest.json
  python3 batch_audit.py --verbose                 # full per-file details
  python3 batch_audit.py --report-only             # JSON only, no terminal
"""

import os
import sys
import json
import glob
import time
import argparse
from datetime import datetime, timezone
from collections import defaultdict, Counter

# ── ANSI colors for terminal ──────────────────────────────────────────────
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

VALID_FILE_STATES = {
    'undone', 'chunking', 'queueing_1', 'summarizing', 'queueing_2',
    'embedding', 'queueing_3', 'db_inserting', 'done', 'failed', 'failed_permanent',
}
VALID_CHUNK_STATES = {'pending', 'done', 'failed', 'failed_permanent', 'undone'}
ACTIVE_STATES = {'chunking', 'summarizing', 'embedding', 'db_inserting'}
TERMINAL_STATES = {'done', 'failed_permanent'}
FAILURE_STATES = {'failed', 'failed_permanent'}
MAX_VALID_RETRY = 3
TIMELY_THRESHOLD_SEC = 600  # match config watchdog.max_working_time_sec


def _normalize(data):
    if isinstance(data, dict) and 'files' in data:
        return data['files']
    if isinstance(data, list):
        return data
    return []


def _load_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        return {"_error": f"Invalid JSON: {e}"}
    except Exception as e:
        return {"_error": str(e)}


def _fmt_time(ts):
    return datetime.fromisoformat(ts.replace('Z', '+00:00')).isoformat() if ts else "N/A"


def _sec_ago(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


def _grade(value, thresholds):
    for score, (lo, hi) in thresholds.items():
        if lo is None and value <= hi:
            return score
        if hi is None and value >= lo:
            return score
        if lo is not None and hi is not None and lo <= value <= hi:
            return score
    return 'F'


def _c(s, color):
    return f"{color}{s}{RESET}"


# ═══════════════════════════════════════════════════════════════════════════
#  Audit Functions — each returns (issues: list, score: float 0-100)
# ═══════════════════════════════════════════════════════════════════════════

def audit_schema(items, batch_file):
    """A. Schema & Structure Validation"""
    issues = []
    if not isinstance(items, list):
        return [{"severity": "CRITICAL", "check": "A0", "msg": f"Top-level data is {type(items).__name__}, not list"}], 0

    n = len(items)
    if n == 0:
        return [{"severity": "WARN", "check": "A0", "msg": "Empty batch_status file (0 entries)"}], 50

    required_fields = {'file_id', 'status', 'last_updated', 'retry_count', 'chunks'}
    optional_with_default = {'total_chunks', 'error_log', 'diagnostics', 'filename_srt', 'filename_mp3', 'file_path'}

    for item in items:
        fid = item.get('file_id', '?')
        missing = required_fields - set(item.keys())
        if missing:
            issues.append({"severity": "ERROR", "check": "A1", "file_id": fid, "msg": f"Missing required fields: {missing}"})

        status = item.get('status', '')
        if status and status not in VALID_FILE_STATES:
            issues.append({"severity": "ERROR", "check": "A2", "file_id": fid, "msg": f"Unknown file status: '{status}"})
        if not status:
            issues.append({"severity": "WARN", "check": "A2", "file_id": fid, "msg": "Empty file status"})

        ts = item.get('last_updated', '')
        if ts:
            try:
                datetime.fromisoformat(ts.replace('Z', '+00:00'))
            except Exception:
                issues.append({"severity": "WARN", "check": "A3", "file_id": fid, "msg": f"Invalid timestamp: {ts}"})
        else:
            issues.append({"severity": "WARN", "check": "A3", "file_id": fid, "msg": "Missing last_updated"})

        chunks = item.get('chunks', [])
        if not isinstance(chunks, list):
            issues.append({"severity": "ERROR", "check": "A4", "file_id": fid, "msg": "'chunks' is not a list"})
        else:
            for ci, c in enumerate(chunks):
                if not isinstance(c, dict):
                    issues.append({"severity": "ERROR", "check": "A4", "file_id": fid, "msg": f"chunks[{ci}] is not a dict"})
                    continue
                if 'chunk_id' not in c:
                    issues.append({"severity": "WARN", "check": "A4", "file_id": fid, "msg": f"chunks[{ci}] missing chunk_id"})
                chunk_st = c.get('status', '')
                if chunk_st and chunk_st not in VALID_CHUNK_STATES:
                    issues.append({"severity": "WARN", "check": "A4", "file_id": fid, "msg": f"chunks[{ci}] unknown status: '{chunk_st}'"})

    score = max(0, 100 - len(issues) * 10)
    return issues, min(score, 100)


def audit_timeliness(items):
    """B. Timeliness & Stuck-State Detection"""
    issues = []
    now = datetime.now(timezone.utc)
    stuck_count = 0
    total = len(items)
    fresh_count = 0

    for item in items:
        fid = item.get('file_id', '?')
        status = item.get('status', '')
        ts = item.get('last_updated', '')
        age = _sec_ago(ts)

        if age is None:
            issues.append({"severity": "WARN", "check": "B1", "file_id": fid, "msg": "Cannot compute last_updated age"})
            continue

        if status in ACTIVE_STATES:
            if age > TIMELY_THRESHOLD_SEC:
                issues.append({"severity": "ERROR", "check": "B2", "file_id": fid,
                               "msg": f"Stuck in '{status}' for {age:.0f}s (>{TIMELY_THRESHOLD_SEC}s)"})
                stuck_count += 1
            elif age > TIMELY_THRESHOLD_SEC * 0.7:
                issues.append({"severity": "WARN", "check": "B2", "file_id": fid,
                               "msg": f"Near-stuck in '{status}' for {age:.0f}s"})

        if status not in TERMINAL_STATES and age > TIMELY_THRESHOLD_SEC * 2:
            issues.append({"severity": "WARN", "check": "B3", "file_id": fid,
                           "msg": f"Non-terminal status '{status}' untouched for {age:.0f}s"})

        if status == 'done':
            diag = item.get('diagnostics') or {}
            if not diag:
                issues.append({"severity": "WARN", "check": "B4", "file_id": fid, "msg": "Done but missing diagnostics block"})
            elif not diag.get('summary'):
                issues.append({"severity": "WARN", "check": "B4", "file_id": fid, "msg": "Done but diagnostics.summary missing"})

        if age is not None and age <= TIMELY_THRESHOLD_SEC:
            fresh_count += 1

    freshness_pct = (fresh_count / total * 100) if total else 100
    score = 100
    score -= stuck_count * 30
    if freshness_pct < 50:
        score -= 20
    return issues, max(score, 0)


def audit_errors(items):
    """C. Error Visibility & Completeness"""
    issues = []
    total_chunks = 0
    failed_chunks = 0
    chunks_missing_error_detail = 0
    items_with_diagnostics = 0

    for item in items:
        fid = item.get('file_id', '?')
        chunks = item.get('chunks', [])
        total_chunks += len(chunks)

        for ci, c in enumerate(chunks):
            status = c.get('status', '')
            if status in FAILURE_STATES:
                failed_chunks += 1
                err = c.get('errors') or c.get('error')
                model = c.get('model_used', '')
                rc = c.get('retry_count', 0)
                if not err:
                    chunks_missing_error_detail += 1
                    issues.append({"severity": "WARN", "check": "C1", "file_id": fid,
                                   "msg": f"chunks[{ci}] failed but has no error detail"})
                if not model:
                    issues.append({"severity": "WARN", "check": "C2", "file_id": fid,
                                   "msg": f"chunks[{ci}] failed but model_used is empty"})
                if rc == 0:
                    issues.append({"severity": "WARN", "check": "C3", "file_id": fid,
                                   "msg": f"chunks[{ci}] failed but retry_count is 0"})

        if item.get('status') in FAILURE_STATES:
            err_log = item.get('error_log', [])
            if not err_log:
                issues.append({"severity": "WARN", "check": "C4", "file_id": fid,
                               "msg": "File failed_permanent but error_log is empty"})

        diag = item.get('diagnostics') or {}
        if diag and diag.get('summary'):
            items_with_diagnostics += 1
            models_diag = diag.get('models', {})
            actual_models = defaultdict(lambda: {"success": 0, "fail": 0})
            for c in chunks:
                m = c.get('model_used', '?')
                if c.get('status') == 'done':
                    actual_models[m]["success"] += 1
                elif c.get('status') in FAILURE_STATES:
                    actual_models[m]["fail"] += 1
            for m, counts in actual_models.items():
                dm = models_diag.get(m, {})
                if dm.get('success', 0) != counts['success'] or dm.get('fail', 0) != counts['fail']:
                    issues.append({"severity": "WARN", "check": "C5", "file_id": fid,
                                   "msg": f"Model '{m}' diagnostics mismatch: diag({dm.get('success',0)}/{dm.get('fail',0)}) "
                                          f"!= actual({counts['success']}/{counts['fail']})"})

    error_capture_rate = ((failed_chunks - chunks_missing_error_detail) / failed_chunks * 100) if failed_chunks else 100
    score = error_capture_rate
    if items and items_with_diagnostics == 0:
        score -= 10
    return issues, max(score, 0)


def audit_integrity(items):
    """D. Data Integrity"""
    issues = []
    for item in items:
        fid = item.get('file_id', '?')
        chunks = item.get('chunks', [])
        tc = item.get('total_chunks', 0)

        if tc != len(chunks):
            issues.append({"severity": "ERROR", "check": "D1", "file_id": fid,
                           "msg": f"total_chunks={tc} != len(chunks)={len(chunks)}"})

        seen_ids = set()
        for ci, c in enumerate(chunks):
            cid = c.get('chunk_id')
            if cid is not None:
                if cid in seen_ids:
                    issues.append({"severity": "ERROR", "check": "D2", "file_id": fid,
                                   "msg": f"Duplicate chunk_id={cid}"})
                seen_ids.add(cid)

            rc = c.get('retry_count', 0)
            if rc > MAX_VALID_RETRY:
                issues.append({"severity": "WARN", "check": "D3", "file_id": fid,
                               "msg": f"chunks[{ci}] retry_count={rc} exceeds max {MAX_VALID_RETRY}"})

        done_c = sum(1 for c in chunks if c.get('status') == 'done')
        fail_c = sum(1 for c in chunks if c.get('status') in FAILURE_STATES)
        pend_c = sum(1 for c in chunks if c.get('status') in ('pending', 'undone'))
        if chunks and done_c + fail_c + pend_c != len(chunks):
            issues.append({"severity": "WARN", "check": "D4", "file_id": fid,
                           "msg": f"Chunk status sum ({done_c}+{fail_c}+{pend_c}) != total ({len(chunks)})"})

        file_rc = item.get('retry_count', 0)
        if file_rc > MAX_VALID_RETRY:
            issues.append({"severity": "WARN", "check": "D3", "file_id": fid,
                           "msg": f"File retry_count={file_rc} exceeds max {MAX_VALID_RETRY}"})

    score = max(0, 100 - len(issues) * 15)
    return issues, max(score, 100)


def audit_model_perf(items):
    """E. Model Performance Statistics (detailed per-model breakdown)"""
    issues = []
    model_agg = defaultdict(lambda: {
        "success": 0, "fail": 0, "total_chars": 0,
        "errors": defaultdict(int), "retries": 0, "total_elapsed": 0.0
    })
    file_model_stats = {}

    for item in items:
        fid = item.get('file_id', '?')
        chunks = item.get('chunks', [])
        file_models = defaultdict(lambda: {"success": 0, "fail": 0, "chars": 0, "errors": defaultdict(int), "retries": 0, "total_elapsed": 0.0})

        for c in chunks:
            m = c.get('model_used', '?')
            st = c.get('status', '')
            cc = c.get('char_count', 0)
            rc = c.get('retry_count', 0)
            ec = c.get('elapsed_sec') or 0
            errs = c.get('errors') or c.get('error') or []

            if st == 'done':
                model_agg[m]["success"] += 1
                file_models[m]["success"] += 1
                model_agg[m]["total_elapsed"] += ec
                file_models[m]["total_elapsed"] += ec
            elif st in FAILURE_STATES:
                model_agg[m]["fail"] += 1
                file_models[m]["fail"] += 1

            model_agg[m]["total_chars"] += cc
            model_agg[m]["retries"] += rc
            file_models[m]["chars"] += cc
            file_models[m]["retries"] += rc

            if isinstance(errs, list):
                for e in errs:
                    if isinstance(e, dict):
                        msg = e.get('message', str(e))[:120]
                        model_agg[m]["errors"][msg] += 1
                        file_models[m]["errors"][msg] += 1
                    else:
                        model_agg[m]["errors"][str(e)[:120]] += 1
                        file_models[m]["errors"][str(e)[:120]] += 1
            elif isinstance(errs, str) and errs:
                model_agg[m]["errors"][errs[:120]] += 1
                file_models[m]["errors"][errs[:120]] += 1

        file_model_stats[fid] = dict(file_models)

    total_calls = sum(m["success"] + m["fail"] for m in model_agg.values())
    total_fails = sum(m["fail"] for m in model_agg.values())
    failure_rate = (total_fails / total_calls * 100) if total_calls else 0
    if failure_rate > 20:
        issues.append({"severity": "WARN", "check": "E1",
                       "msg": f"Overall failure rate is {failure_rate:.1f}% ({total_fails}/{total_calls})"})

    for m, s in model_agg.items():
        total = s["success"] + s["fail"]
        rate = (s["success"] / total * 100) if total else 0
        if total >= 5 and rate < 50:
            issues.append({"severity": "ERROR", "check": "E2",
                           "msg": f"Model '{m}' success rate is {rate:.1f}% ({s['success']}/{total})"})

    score = 100
    if failure_rate > 20:
        score -= 20
    for m, s in model_agg.items():
        total = s["success"] + s["fail"]
        rate = (s["success"] / total * 100) if total else 0
        if total >= 5 and rate < 50:
            score -= 15
    return issues, file_model_stats, max(score, 0)


def audit_cross_file(items, master_path):
    """F. Cross-File Consistency against master_file_manifest.json"""
    issues = []
    if not master_path or not os.path.exists(master_path):
        issues.append({"severity": "INFO", "check": "F0", "msg": f"Master manifest not found at '{master_path}', skipping cross-file check"})
        return issues, 100

    master = _load_json(master_path)
    if "_error" in master:
        issues.append({"severity": "WARN", "check": "F0", "msg": f"Cannot read master manifest: {master['_error']}"})
        return issues, 100

    master_files = master.get('files', [])
    master_ids = {f['id'] for f in master_files}

    for item in items:
        fid = item.get('file_id')
        if fid is not None and fid not in master_ids:
            issues.append({"severity": "ERROR", "check": "F1", "file_id": fid,
                           "msg": f"file_id {fid} not found in master_file_manifest"})

    batch_ids = {item.get('file_id') for item in items if item.get('file_id') is not None}
    missing_from_batch = master_ids - batch_ids
    if missing_from_batch:
        issues.append({"severity": "INFO", "check": "F2",
                       "msg": f"Files in manifest but not in this batch: {sorted(missing_from_batch)[:10]}..."})

    score = max(0, 100 - len(issues) * 15)
    return issues, max(score, 100)


# ═══════════════════════════════════════════════════════════════════════════
#  Report Generation
# ═══════════════════════════════════════════════════════════════════════════

def _build_per_file_detail(items, file_model_stats):
    """Build per-file details matching user's requested format."""
    details = []
    for item in items:
        fid = item.get('file_id', '?')
        chunks = item.get('chunks', [])
        fm = file_model_stats.get(fid, {})

        chunk_details = []
        for c in chunks:
            m = c.get('model_used', '?')
            ms = fm.get(m, {})
            total = ms.get("success", 0) + ms.get("fail", 0)
            sr = round(ms.get("success", 0) / total * 100, 1) if total else 0
            cps = round(ms.get("chars", 0) / ms.get("total_elapsed", 1), 1) if ms.get("total_elapsed", 0) > 0 else None
            errs = c.get('errors') or c.get('error') or []
            err_obj = None
            if isinstance(errs, list) and errs:
                first = errs[0]
                if isinstance(first, dict):
                    err_obj = {"model": first.get("model", m), "message": first.get("message", str(first))}
                else:
                    err_obj = {"model": m, "message": str(first)}
            elif isinstance(errs, str) and errs:
                err_obj = {"model": m, "message": errs}

            chunk_details.append({
                "chunk_id": c.get('chunk_id'),
                "status": c.get('status', '?'),
                "model_used": m,
                "retry_count": c.get('retry_count', 0),
                "char_count": c.get('char_count', 0),
                "elapsed_sec": c.get('elapsed_sec'),
                "model_stats": {
                    "success": ms.get("success", 0),
                    "fail": ms.get("fail", 0),
                    "success_rate": sr,
                    "chars_per_sec": cps,
                    "errors": dict(ms.get("errors", {}))
                },
                "error": err_obj,
                "summary_preview": c.get('summary_preview', '')[:80]
            })

        diag = item.get('diagnostics') or {}
        top_failed = []
        for m, s in sorted(fm.items(), key=lambda x: -x[1].get("fail", 0)):
            if s.get("fail", 0) > 0:
                top_failed.append({"model": m, "fail_count": s["fail"],
                                   "top_errors": [{"msg": k, "count": v} for k, v in
                                                  sorted(s["errors"].items(), key=lambda x: -x[1])[:5]]})

        details.append({
            "file_id": fid,
            "filename_srt": item.get('filename_srt', ''),
            "filename_mp3": item.get('filename_mp3', ''),
            "status": item.get('status', '?'),
            "retry_count": item.get('retry_count', 0),
            "last_updated": item.get('last_updated', ''),
            "total_chunks": item.get('total_chunks', 0),
            "chunks": chunk_details,
            "diagnostics": diag,
            "error_log": item.get('error_log', []),
            "audit": {
                "model_performance": {m: {"success": s["success"], "fail": s["fail"],
                                          "chars": s.get("chars", 0),
                                          "chars_per_sec": round(s.get("chars", 0) / s.get("total_elapsed", 1), 1) if s.get("total_elapsed", 0) > 0 else None,
                                          "top_errors": [{"msg": k, "count": v} for k, v in
                                                         sorted(s["errors"].items(), key=lambda x: -x[1])[:5]]}
                                      for m, s in sorted(fm.items())},
                "top_failed_models": top_failed
            }
        })
    return details


def _color_status(st):
    if st == 'done':
        return _c(st, GREEN)
    if st in FAILURE_STATES:
        return _c(st, RED)
    if st in ACTIVE_STATES:
        return _c(st, YELLOW)
    return st


def _print_terminal_report(summary, all_issues, file_details, verbose):
    print(f"\n{'='*70}")
    print(f"{BOLD}  BATCH STATUS AUDIT REPORT{RESET}")
    print(f"  Generated: {summary['timestamp']}")
    print(f"{'='*70}")

    # ── Overall Score ──
    score = summary["overall_score"]
    color = GREEN if score >= 90 else YELLOW if score >= 70 else RED
    print(f"\n{BOLD}OVERALL AUDIT SCORE: {_c(f'{score:.1f}/100', color)}{RESET}")
    grade = 'A' if score >= 90 else 'B' if score >= 80 else 'C' if score >= 70 else 'D' if score >= 50 else 'F'
    print(f"  Grade: {_c(grade, color)}")
    print(f"  Files: {summary['total_files']} | Done: {summary['done_files']} | "
          f"Failed: {summary['failed_files']} | Active: {summary['active_files']}")
    print(f"  Chunks: {summary['total_chunks']} total, {summary['failed_chunks']} failed")
    print(f"  Overall failure rate: {summary['failure_rate']:.1f}%")
    print(f"  Freshness: {summary['freshness_pct']:.0f}% ({summary['fresh_count']}/{summary['total_files']} fresh)")

    # ── Per-Dimension Scores ──
    print(f"\n{BOLD}PER-DIMENSION SCORES:{RESET}")
    for dim in summary['dimensions']:
        dscore = dim['score']
        dcolor = GREEN if dscore >= 90 else YELLOW if dscore >= 70 else RED
        print(f"  [{dim['letter']}] {dim['name']:<35} {_c(f'{dscore:.1f}/100', dcolor)}  "
              f"({dim['issue_count']} issues)")

    # ── All Issues ──
    if all_issues:
        print(f"\n{BOLD}ISSUES FOUND:{RESET}")
        for iss in sorted(all_issues, key=lambda x: (0 if x['severity'] == 'CRITICAL' else 1 if x['severity'] == 'ERROR' else 2 if x['severity'] == 'WARN' else 3, x.get('check', ''))):
            sev_color = RED if iss['severity'] == 'CRITICAL' else YELLOW if iss['severity'] == 'ERROR' else CYAN if iss['severity'] == 'WARN' else RESET
            fid_info = f" [fid={iss.get('file_id','?')}]" if 'file_id' in iss else ""
            tag = f"[{iss['severity']}]"
            print(f"  {_c(tag, sev_color)} {iss.get('check','')}{fid_info}: {iss['msg']}")
    else:
        print(f"\n{GREEN}No issues found!{RESET}")

    # ── Model Performance Summary ──
    if summary.get('model_performance'):
        print(f"\n{BOLD}MODEL PERFORMANCE SUMMARY:{RESET}")
        print(f"  {'Model':<42} {'Success':>7} {'Fail':>5} {'Rate':>7} {'Chars':>8} {'CPS':>8}")
        print(f"  {'-'*42} {'-'*7} {'-'*5} {'-'*7} {'-'*8} {'-'*8}")
        for m, s in sorted(summary['model_performance'].items(), key=lambda x: -x[1]['fail']):
            total = s['success'] + s['fail']
            rate = f"{s['success']/total*100:.1f}%" if total else "N/A"
            cps = f"{s['chars_per_sec']:.1f}" if s.get('chars_per_sec') else "N/A"
            print(f"  {m:<42} {s['success']:>7} {s['fail']:>5} {rate:>7} {s['total_chars']:>8} {cps:>8}")
            if s.get('top_errors'):
                for e in s['top_errors'][:3]:
                    print(f"  {'':>42}  -> {e['msg'][:70]} ({e['count']}x)")

    # ── Top Failed Models ──
    top_failed_global = summary.get('top_failed_models', [])
    if top_failed_global:
        print(f"\n{BOLD}TOP FAILED MODELS:{RESET}")
        for i, entry in enumerate(top_failed_global, 1):
            print(f"  #{i} {entry['model']:<42} {entry['fail_count']} fails")
            for e in entry.get('top_errors', [])[:3]:
                print(f"     -> {e['msg'][:80]} ({e['count']}x)")

    # ── Per-File Details (verbose) ──
    if verbose and file_details:
        print(f"\n{BOLD}PER-FILE DETAILS:{RESET}")
        for fd in file_details:
            print(f"\n  {'─'*60}")
            print(f"  File ID: {fd['file_id']} | Status: {_color_status(fd['status'])} | "
                  f"Chunks: {fd['total_chunks']} | Retry: {fd['retry_count']}")
            if fd.get('filename_srt'):
                print(f"  SRT: {fd['filename_srt']}")
            if fd.get('error_log'):
                for e in fd['error_log']:
                    print(f"  {_c('[ERROR_LOG]', RED)} {e.get('error','')[:100]}")
            diag = fd.get('diagnostics', {})
            if diag.get('summary'):
                s = diag['summary']
                print(f"  Diagnostics: {s.get('records',0)} records, "
                      f"{s.get('elapsed_sec','?')}s, {s.get('chars_per_sec','?')} cps")
            if fd.get('chunks'):
                failed = [ch for ch in fd['chunks'] if ch.get('status') in FAILURE_STATES]
                if failed:
                    print(f"  {_c(f'Failed chunks: {len(failed)}', RED)}")
                    for ch in failed[:5]:
                        e = ch.get('error') or {}
                        print(f"    chunk {ch['chunk_id']}: model={ch['model_used']} "
                              f"rc={ch['retry_count']} err={e.get('message','')[:80] if isinstance(e,dict) else str(e)[:80]}")
            mp = fd.get('audit', {}).get('model_performance', {})
            if mp:
                print(f"  Model Performance:")
                for m, s in sorted(mp.items(), key=lambda x: -x[1].get('fail', 0)):
                    total = s['success'] + s['fail']
                    rate = f"{s['success']/total*100:.1f}%" if total else "N/A"
                    cps = f"{s['chars_per_sec']:.1f}" if s.get('chars_per_sec') else "N/A"
                    print(f"    {m:<35} success={s['success']} fail={s['fail']} rate={rate} cps={cps}")
                    for e in s.get('top_errors', [])[:2]:
                        print(f"      -> {e['msg'][:70]} ({e['count']}x)")
    print(f"\n{'='*70}\n")


def build_report(batch_files, master_file, verbose):
    issues_all = []
    dim_scores = []
    total_items = 0
    done_files = 0
    active_files = 0
    failed_files = 0
    total_chunks = 0
    failed_chunks = 0
    fresh_count = 0
    all_items = []
    file_model_stats_all = {}

    for bf in batch_files:
        raw = _load_json(bf)
        if "_error" in raw:
            issues_all.append({"severity": "ERROR", "check": "A0", "msg": f"Cannot read {bf}: {raw['_error']}"})
            continue
        items = _normalize(raw)
        if not items:
            issues_all.append({"severity": "WARN", "check": "A0", "msg": f"No items in {bf}"})
            continue

        for item in items:
            all_items.append(item)

        iss_s, score_s = audit_schema(items, bf)
        iss_b, score_b = audit_timeliness(items)
        iss_c, score_c = audit_errors(items)
        iss_d, score_d = audit_integrity(items)
        iss_e, file_ms, score_e = audit_model_perf(items)
        iss_f, score_f = audit_cross_file(items, master_file)

        issues_all.extend(iss_s)
        issues_all.extend(iss_b)
        issues_all.extend(iss_c)
        issues_all.extend(iss_d)
        issues_all.extend(iss_e)
        issues_all.extend(iss_f)

        file_model_stats_all.update(file_ms)

        dim_scores.extend([
            {"letter": "A", "name": "Schema & Structure", "score": score_s, "issue_count": len(iss_s)},
            {"letter": "B", "name": "Timeliness", "score": score_b, "issue_count": len(iss_b)},
            {"letter": "C", "name": "Error Visibility", "score": score_c, "issue_count": len(iss_c)},
            {"letter": "D", "name": "Data Integrity", "score": score_d, "issue_count": len(iss_d)},
            {"letter": "E", "name": "Model Performance", "score": score_e, "issue_count": len(iss_e)},
            {"letter": "F", "name": "Cross-File Consistency", "score": score_f, "issue_count": len(iss_f)},
        ])

    # Aggregate
    for item in all_items:
        total_items += 1
        st = item.get('status', '')
        if st == 'done':
            done_files += 1
        elif st in ACTIVE_STATES:
            active_files += 1
        elif st in FAILURE_STATES:
            failed_files += 1

        chunks = item.get('chunks', [])
        total_chunks += len(chunks)
        for c in chunks:
            if c.get('status') in FAILURE_STATES:
                failed_chunks += 1

        age = _sec_ago(item.get('last_updated', ''))
        if age is not None and age <= TIMELY_THRESHOLD_SEC:
            fresh_count += 1

    # Model aggregate
    model_agg = defaultdict(lambda: {"success": 0, "fail": 0, "total_chars": 0, "total_elapsed": 0.0, "errors": defaultdict(int)})
    for item in all_items:
        for c in item.get('chunks', []):
            m = c.get('model_used', '?')
            st = c.get('status', '')
            cc = c.get('char_count', 0)
            ec = c.get('elapsed_sec') or 0
            if st == 'done':
                model_agg[m]["success"] += 1
                model_agg[m]["total_elapsed"] += ec
            elif st in FAILURE_STATES:
                model_agg[m]["fail"] += 1
            model_agg[m]["total_chars"] += cc
            errs = c.get('errors') or c.get('error') or []
            if isinstance(errs, list):
                for e in errs:
                    if isinstance(e, dict):
                        model_agg[m]["errors"][e.get('message', str(e))[:120]] += 1
                    else:
                        model_agg[m]["errors"][str(e)[:120]] += 1
            elif isinstance(errs, str) and errs:
                model_agg[m]["errors"][errs[:120]] += 1

    model_perf = {}
    top_failed_global = []
    for m, s in sorted(model_agg.items(), key=lambda x: -x[1]["fail"]):
        cps = round(s["total_chars"] / s["total_elapsed"], 1) if s["total_elapsed"] > 0 else None
        model_perf[m] = {
            "success": s["success"], "fail": s["fail"],
            "total_chars": s["total_chars"],
            "chars_per_sec": cps,
            "top_errors": [{"msg": k, "count": v} for k, v in
                          sorted(s["errors"].items(), key=lambda x: -x[1])[:10]]
        }
        if s["fail"] > 0:
            top_failed_global.append({"model": m, "fail_count": s["fail"],
                                      "top_errors": model_perf[m]["top_errors"]})

    total_calls = sum(m["success"] + m["fail"] for m in model_agg.values())
    failure_rate = (sum(m["fail"] for m in model_agg.values()) / total_calls * 100) if total_calls else 0
    global_score = sum(d["score"] for d in dim_scores) / len(dim_scores) if dim_scores else 0

    file_details = _build_per_file_detail(all_items, file_model_stats_all)

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "batch_files_analyzed": len(batch_files),
        "total_files": total_items,
        "done_files": done_files,
        "active_files": active_files,
        "failed_files": failed_files,
        "fresh_count": fresh_count,
        "freshness_pct": (fresh_count / total_items * 100) if total_items else 0,
        "total_chunks": total_chunks,
        "failed_chunks": failed_chunks,
        "failure_rate": failure_rate,
        "overall_score": round(global_score, 1),
        "dimensions": dim_scores,
        "model_performance": model_perf,
        "top_failed_models": top_failed_global,
    }

    report = {
        "summary": summary,
        "issues": [{"severity": i["severity"], "check": i.get("check",""),
                    "file_id": i.get("file_id"), "msg": i["msg"]} for i in sorted(
            issues_all, key=lambda x: (0 if x['severity'] == 'CRITICAL' else 1 if x['severity'] == 'ERROR' else 2, x.get('check','')))],
        "files": file_details,
    }

    return report, summary, issues_all, file_details


def main():
    parser = argparse.ArgumentParser(description="batch_status_*.json Comprehensive Auditor")
    parser.add_argument("--output-dir", help="Directory containing batch_status_*.json files")
    parser.add_argument("--master-file", help="Path to master_file_manifest.json")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show per-file details")
    parser.add_argument("--report-only", action="store_true", help="Only generate JSON, no terminal output")
    args = parser.parse_args()

    # Determine output directory
    output_dir = args.output_dir
    if not output_dir:
        output_dir = os.environ.get('SRT_OUTPUT_DIR')
    if not output_dir:
        SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
        try:
            for p in [os.path.join(SCRIPT_DIR, '..', 'config.json'), os.path.join(SCRIPT_DIR, 'config.json')]:
                if os.path.exists(p):
                    with open(p, 'r') as f:
                        cfg = json.load(f)
                    output_dir = cfg.get('paths', {}).get('output_dir', '')
                    if output_dir:
                        break
        except Exception:
            pass
    if not output_dir:
        output_dir = './output'

    # Determine master file
    master_file = args.master_file
    if not master_file:
        master_file = os.environ.get('SRT_MASTER_FILE')
    if not master_file:
        SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
        try:
            for p in [os.path.join(SCRIPT_DIR, '..', 'config.json'), os.path.join(SCRIPT_DIR, 'config.json')]:
                if os.path.exists(p):
                    with open(p, 'r') as f:
                        cfg = json.load(f)
                    master_file = cfg.get('paths', {}).get('master_file', '')
                    if master_file:
                        break
        except Exception:
            pass
    if not master_file:
        master_file = './examples/master_file_manifest.example.json'

    if not os.path.isdir(output_dir):
        print(f"{RED}[Error]{RESET} Output directory not found: {output_dir}")
        print("Specify with --output-dir or set SRT_OUTPUT_DIR env var")
        sys.exit(1)

    batch_files = sorted(glob.glob(os.path.join(output_dir, 'batch_status_*.json')))
    if not batch_files:
        print(f"{YELLOW}[Warning]{RESET} No batch_status_*.json files found in {output_dir}")
        sys.exit(0)

    print(f"{CYAN}[Audit]{RESET} Found {len(batch_files)} batch_status file(s) in {output_dir}")
    print(f"{CYAN}[Audit]{RESET} Batch files: {[os.path.basename(b) for b in batch_files]}")

    report, summary, all_issues, file_details = build_report(batch_files, master_file, args.verbose)

    # Write JSON report
    report_filename = f"batch_audit_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path = os.path.join(output_dir, report_filename)
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"{GREEN}[Audit]{RESET} JSON report saved: {report_path}")

    # Terminal output
    if not args.report_only:
        _print_terminal_report(summary, all_issues, file_details, args.verbose)

    # Exit code based on score (10 = minor issues, 20 = critical issues)
    if summary['overall_score'] >= 90:
        sys.exit(0)
    elif summary['overall_score'] >= 70:
        sys.exit(10)
    else:
        sys.exit(20)


if __name__ == '__main__':
    main()
