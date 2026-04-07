-- Run in Supabase SQL Editor
-- Adds doc_type and effective_date to obc_sections so amendments and appeals
-- are clearly labelled and Claude knows how to weight them.

alter table obc_sections
    add column if not exists doc_type      text default 'consolidated_bylaw',
    add column if not exists effective_date date,
    add column if not exists bylaw_number  text;

-- doc_type values:
--   consolidated_bylaw   — base + all amendments merged (preferred source)
--   base_bylaw           — original bylaw, amendments may exist separately
--   amendment            — modifies specific sections of the base/consolidated bylaw
--   amendment_index      — summary list of amendments, reference only
--   olt_appeal           — OLT/OMB appeal decision, may reverse provisions
--   appeal_index         — index of appeals, reference only
--   obc                  — provincial Ontario Building Code
--   permit_guide         — municipal permit application guide
--   other                — anything else

create index if not exists obc_sections_doc_type_idx on obc_sections (doc_type);

comment on column obc_sections.doc_type is
    'Document classification: consolidated_bylaw | base_bylaw | amendment | amendment_index | olt_appeal | appeal_index | obc | permit_guide | other';
comment on column obc_sections.effective_date is
    'Date this document came into effect — used to resolve conflicts (most recent wins)';
comment on column obc_sections.bylaw_number is
    'e.g. "1-88", "2026-50", "Amendment 2019-201" — for citation purposes';
