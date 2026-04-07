-- Run this in Supabase SQL Editor BEFORE running 2_load_to_supabase.py
-- Dashboard → SQL Editor → paste and run

create table if not exists municipalities (
    id                  uuid primary key default gen_random_uuid(),
    name                text not null unique,
    municipality_type   text,           -- city | town | township | village | county | region | other
    region              text,           -- county or district name
    population          integer,
    website_url         text,           -- main municipality website
    building_dept_url   text,           -- building department / permits page  ← key field
    permit_portal_url   text,           -- CloudPermit / Accela / custom portal URL
    platform            text,           -- cloudpermit | accela | amanda | cityview | custom | none
    last_crawled_at     timestamptz,    -- when we last scraped for documents
    active              boolean default true,
    created_at          timestamptz default now(),
    updated_at          timestamptz default now()
);

-- Keep updated_at current automatically
create or replace function set_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists municipalities_updated_at on municipalities;
create trigger municipalities_updated_at
    before update on municipalities
    for each row execute function set_updated_at();

-- Fast lookups by name and region
create index if not exists municipalities_name_idx   on municipalities (name);
create index if not exists municipalities_region_idx on municipalities (region);
create index if not exists municipalities_active_idx on municipalities (active);
