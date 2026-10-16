"""Explicit DIN two-dot cohort: up to 25 SEACE + 25 minor-contract new matches.

Preparation is offline. --execute uses the existing committed CAS/reservation,
compact JEV transport and schema-enforced Flash-Lite shared-batch engines. No
ingestion, queues, replacement, migration, historical replay or deployment.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import run_dinaut_three_dot_matches as run
import run_dinaut_three_dot_gemma_batches as batches
from dinaut_batch_scope import TWO_DOT_50_SCOPE


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder',type=Path,default=run.ROOT/'tmp'/TWO_DOT_50_SCOPE.folder_name)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--limit',type=int)
    parser.add_argument('--jev-only',action='store_true')
    args=parser.parse_args(argv)
    folder=args.folder.resolve()
    if folder!=(run.ROOT/'tmp'/TWO_DOT_50_SCOPE.folder_name).resolve():
        raise ValueError('Use the canonical DIN two-dot folder; never copy paid inputs or receipts')
    if args.limit is not None and not 1<=args.limit<=50:
        raise ValueError('The explicit two-dot limit must be between one and fifty')
    delegated=['--recommendation-model','gemini-3.1-flash-lite']
    if args.execute:delegated.append('--execute')
    if args.limit is not None:delegated.extend(['--limit',str(args.limit)])
    if args.jev_only:
        return run.main([*delegated,'--folder',str(folder),'--jev-only'],scope=TWO_DOT_50_SCOPE)
    return batches.main(delegated,scope=TWO_DOT_50_SCOPE,folder=folder)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        run.emit({'status':'blocked','error_class':type(exc).__name__,
            'reason':str(exc) if isinstance(exc,(ValueError,run.ProviderBlocked)) else 'Inspect private evidence; no automatic retry',
            'automatic_retry':False})
        raise SystemExit(2)
