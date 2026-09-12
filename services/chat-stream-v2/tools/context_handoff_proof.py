"""Validate installed handoff receipts without treating spawn.ok as lineage proof."""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def verify_handoff(manifest: dict, rows: dict, receipt: dict) -> None:
    source = rows[manifest['source']['stream_id']]
    witness = rows[manifest['witness']['stream_id']]
    successor_id = str(receipt['stream_id'])
    successor = rows[successor_id]
    for label, row in (('source', source), ('witness', witness)):
        if row['session_generation'] != manifest[label]['session_generation']:
            raise ValueError(f'{label} generation mismatch')
        if row['created_at'] != manifest[label]['created_at']:
            raise ValueError(f'{label} created_at mismatch')
    delivery = receipt['initial_prompt_delivery']
    if delivery.get('delivery_status') != 'delivered':
        raise ValueError('successor initial prompt not delivered')
    if successor['handoff_from_stream_id'] != source['stream_id']:
        raise ValueError('successor lineage mismatch')
    if witness['parent_stream_id'] != successor_id:
        raise ValueError('witness parentage mismatch')
    if source['status'] != 'closed' or source['close_kind'] != 'handed_off':
        raise ValueError('source not closed by handoff')
    for row in (source, successor):
        if (row['effective_model'], row['effective_effort']) != ('claude-fable-5-1', 'high'):
            raise ValueError('Fable source/successor tuple mismatch')
        if row.get('routing_integrity') == 'mismatch':
            raise ValueError('routing integrity mismatch')
    if successor['status'] != 'open' or witness['status'] != 'open':
        raise ValueError('successor and witness must be live before cleanup')
    if not successor.get('session_generation'):
        raise ValueError('missing successor generation')
    exchange = delivery.get('exchange') or {}
    if exchange.get('child_generation') and exchange['child_generation'] != successor['session_generation']:
        raise ValueError('successor receipt generation mismatch')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifact_directory', type=Path)
    args = parser.parse_args()
    root = args.artifact_directory
    verify_handoff(*(json.loads((root/name).read_text()) for name in (
        'handoff-manifest.json', 'lead-lineage-readback.json', 'successor-delivery.json')))
    print('PASS: installed Fable handoff delivery, generations and child parentage')


if __name__ == '__main__':
    main()
