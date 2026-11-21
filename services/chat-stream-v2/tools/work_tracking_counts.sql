SELECT 'alert' AS metric, NULL AS spec_id,
       json_extract(c.value, '$.condition') AS condition,
       json_extract(c.value, '$.event_kind') AS event_kind,
       json_extract(c.value, '$.seat_role') AS seat_role,
       json_extract(c.value, '$.gate') AS gate,
       json_extract(c.value, '$.gate_outcome') AS gate_outcome,
       json_extract(n.metadata, '$.delivery_type') AS delivery_type,
       count(DISTINCT n.notice_id) AS total
FROM v2_outbound_notices AS n, json_each(n.metadata, '$.tracking_causes') AS c
WHERE n.kind = 'work_item_tracking' AND n.delivered_at IS NOT NULL
GROUP BY 3,4,5,6,7,8
UNION ALL
SELECT 'seat_only', json_extract(r.usage_snapshot, '$.attribution.spec_id'),
       NULL, NULL, NULL, NULL, NULL, NULL, count(DISTINCT r.report_id)
FROM v2_reports AS r
WHERE r.status = 'done' AND r.completion_kind = 'seat_only'
  AND json_extract(r.usage_snapshot, '$.attribution.mode') = 'exclusive_primary'
GROUP BY json_extract(r.usage_snapshot, '$.attribution.spec_id');
