-- Temporal contract: recorded_at >= occurred_at
select *
from {{ ref('events') }}
where recorded_at < occurred_at
