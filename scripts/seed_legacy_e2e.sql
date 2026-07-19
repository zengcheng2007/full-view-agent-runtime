-- Local development fixture for the legacy geo-qxst adapter.
-- Run only against the disposable local geo_qxst database.

DO $$
BEGIN
    IF to_regclass('public.dm_empty_nest_old') IS NULL THEN
        EXECUTE $view$
            CREATE VIEW public.dm_empty_nest_old AS
            SELECT *
            FROM public.base_ppl_older
            WHERE living_conditions = '01'
        $view$;
    END IF;
END
$$;

INSERT INTO public.base_ppl_older (
    uuid,
    living_conditions,
    city_code,
    county_code,
    town_code
)
VALUES
    ('agent-e2e-elder-001', '01', '330100', '330106', '330106002'),
    ('agent-e2e-elder-002', '01', '330100', '330106', '330106002'),
    ('agent-e2e-elder-003', '01', '330100', '330106', '330106003'),
    ('agent-e2e-elder-004', '01', '330100', '330106', '330106004')
ON CONFLICT (uuid) DO NOTHING;
