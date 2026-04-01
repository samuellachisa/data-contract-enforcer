-- Fails rows where per-fact confidence is outside [0,1] (contract clause extracted_facts.confidence).
select *
from {{ ref('extraction_facts') }}
where confidence is null or confidence < 0 or confidence > 1
