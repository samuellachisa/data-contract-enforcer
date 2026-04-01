-- DocumentProcessed payload.bytes >= 0
select *
from {{ ref('event_document_processed_payload') }}
where bytes < 0
