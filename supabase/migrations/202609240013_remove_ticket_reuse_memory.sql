-- Ticket references are issue-scoped, not reusable customer memory. Remove
-- legacy escalation hints that told the model to reuse an unrelated ticket.
update public.memory_summaries ms
set summary=jsonb_set(
  jsonb_set(ms.summary,'{escalations}','[]'::jsonb,true),
  '{actions}',
  coalesce((select jsonb_agg(value)
            from jsonb_array_elements_text(coalesce(ms.summary->'actions','[]'::jsonb)) value
            where value not like 'Escalated to a human%'),'[]'::jsonb),
  true
);
