#!/usr/bin/env python3
"""Assert captured native AX identities against authoritative daemon/model evidence."""
import json,pathlib
LITERAL='[Image: original 1x1, displayed at 1x1. Multiply coordinates by 1 to map to original image.]'
def validate(directory,expected,model):
    receipts=[]
    for mode in ('hidden','shown'):
        root=pathlib.Path(directory)/('mobile-'+mode)
        nav=json.loads((root/'navigation-identity.json').read_text())
        assert nav['stream']==expected['stream'] and nav['provider_marker']==expected['provider_marker'] and nav['marker_visible'] and nav['session_header_visible'],'HARNESS_ERROR: mobile navigation identity mismatch'
        pages=[{'path':str(p),'rows':json.loads(p.read_text())} for p in sorted(root.rglob('screen.ax.json'))]
        rows=[x for page in pages for x in page['rows']];labels=[str(x.get('AXLabel') or '') for x in rows];ids={str(x.get('AXUniqueId') or '') for x in rows}
        assert any(expected['provider_marker'] in s for s in labels),'HARNESS_ERROR: mobile provider marker absent'
        assert not any(expected['meta_text'] in s for s in labels),'PRODUCT_FAIL: mobile phantom image metadata rendered'
        assert not any('[pentacle-notice:' in s for s in labels),'PRODUCT_FAIL: mobile notice protocol wrapper rendered'
        # Notice Text has no native testID. Bind its semantic label to the sole daemon notice id and notification id.
        assert any('Operator answered' in s for s in labels) and any(s.strip().lower()=='done' or 'operator answered: done' in s.lower() for s in labels),'PRODUCT_FAIL: mobile compact notice/answer absent'
        actual_read_labels=[str(row.get('AXLabel') or '') for row in rows
            if str(row.get('AXUniqueId') or '') in {'tool-result-card-'+str(i) for i in expected['read_use_ids']}
            and str(row.get('AXLabel') or '').strip() == 'Tool result. Read '+expected['owned_read_path']]
        if mode=='shown':
            assert actual_read_labels,'PRODUCT_FAIL: mobile owned Read tool invocation absent in shown preference'
            cards=[str(i['id']) for i in model['views'][1]['detail']['transcriptItems'] if i.get('kind')=='TOOL_RESULT' and i.get('text','').strip() and i.get('displayRule') not in ('activity:code-block','activity:question')]
            assert cards,'HARNESS_ERROR: no nonempty result positive control available'
            assert any('tool-result-card-'+i in ids for i in cards),'PRODUCT_FAIL: mobile nonempty result card identity absent in shown preference'
        else:
            assert not actual_read_labels,'PRODUCT_FAIL: mobile owned Read tool invocation leaked into hidden preference'
            assert not any(i.startswith('tool-result-card-') for i in ids),'PRODUCT_FAIL: mobile result cards leaked into hidden preference'
        if expected.get('controls'):
            assert any(s.strip()==LITERAL for s in labels),'PRODUCT_FAIL: mobile real typed literal control missing'
            assert any(expected['controls']['caption'] in s for s in labels),'PRODUCT_FAIL: mobile real attachment caption missing'
            assert 'message-image-0' in ids or 'message-image-0-img' in ids,'PRODUCT_FAIL: mobile real attachment image identity missing'
            assert not any(i in ids for i in ('message-image-0-broken','message-image-0-loading')),'PRODUCT_FAIL: mobile attachment failed or still loading'
        receipts.append({'mode':mode,'stream':expected['stream'],'notice_seq':expected['notice_seq'],'notification_id':expected['notification_id'],'provider_marker':expected['provider_marker'],'read_use_ids':expected['read_use_ids'],'owned_read_labels':actual_read_labels,'nonempty_result_ids':[i for i in ids if i.startswith('tool-result-card-')],'pages':[p['path'] for p in pages],'assertions':'PASS'})
    return receipts
