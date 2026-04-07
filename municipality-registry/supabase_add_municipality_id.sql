-- Run this in Supabase SQL Editor
-- Adds municipality_id to obc_sections so chunks can be tagged to a specific
-- municipality. NULL = provincial OBC (applies everywhere).

alter table obc_sections
    add column if not exists municipality_id uuid references municipalities(id);

-- Index for fast municipality-filtered searches
create index if not exists obc_sections_municipality_idx
    on obc_sections (municipality_id);

-- Update the match function to support optional municipality filtering.
-- Returns provincial chunks (municipality_id IS NULL) PLUS chunks for the
-- requested municipality, ranked by similarity.
create or replace function match_obc_sections(
    query_embedding vector(1536),
    match_count     int,
    p_municipality_id uuid default null
)
returns table (
    id             uuid,
    section_number text,
    title          text,
    content        text,
    municipality_id uuid,
    similarity     float
)
language sql stable
as $$
    select
        id,
        section_number,
        title,
        content,
        municipality_id,
        1 - (embedding <=> query_embedding) as similarity
    from obc_sections
    where
        municipality_id is null                          -- always include OBC
        or municipality_id = p_municipality_id           -- plus requested municipality
    order by embedding <=> query_embedding
    limit match_count;
$$;
