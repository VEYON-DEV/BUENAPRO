"""Explicit DIN cohort: up to thirty new SEACE PROD2 two-dot matches only.

Preparation is offline. --execute uses the same committed JEV/Flash-Lite engine,
CAS, paid receipts and shared DIN coordinator lock in its own immutable folder.
No minor contracts, replacements, ingestion, queues or deployment are authorized.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import run_dinaut_three_dot_matches as run
import run_dinaut_three_dot_gemma_batches as batches
from dinaut_batch_scope import TWO_DOT_SEACE_30_SCOPE


def main(argv=None):
    scope=TWO_DOT_SEACE_30_SCOPE
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder',type=Path,default=run.ROOT/'tmp'/scope.folder_name)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--limit',type=int)
    parser.add_argument('--jev-only',action='store_true')
    args=parser.parse_args(argv)
    folder=args.folder.resolve()
    if folder!=(run.ROOT/'tmp'/scope.folder_name).resolve():
        raise ValueError('Use the canonical SEACE-only DIN folder; never copy paid inputs or receipts')
    if args.limit is not None and not 1<=args.limit<=scope.maximum_cases:
        raise ValueError('The SEACE-only explicit limit must be between one and thirty')
    delegated=['--recommendation-model','gemini-3.1-flash-lite']
    if args.execute:delegated.append('--execute')
    if args.limit is not None:delegated.extend(['--limit',str(args.limit)])
    if args.jev_only:
        return run.main([*delegated,'--folder',str(folder),'--jev-only'],scope=scope)
    return batches.main(delegated,scope=scope,folder=folder)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        run.emit({'status':'blocked','error_class':type(exc).__name__,
            'reason':str(exc) if isinstance(exc,(ValueError,run.ProviderBlocked)) else 'Inspect private evidence; no automatic retry',
            'automatic_retry':False})
        raise SystemExit(2)
