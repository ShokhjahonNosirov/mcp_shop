-- Fictional seed data. All names are invented and all emails use the
-- reserved example.com domain (RFC 2606), so nothing here is real.
--
-- The data is deterministic: instead of random() we hash a label with md5,
-- so the same rows (and the same bestsellers, revenue shape, etc.) are
-- produced on every fresh database. Dates are relative to the time the
-- database is initialised, so "last 30 days" always has data in a demo.

-- Deterministic "random" number in [0, 1) derived from a text label.
CREATE FUNCTION pg_temp.rnd(label text) RETURNS double precision
LANGUAGE sql IMMUTABLE AS $$
    SELECT ('x' || lpad(substr(md5(label), 1, 8), 16, '0'))::bit(64)::bigint / 4294967296.0
$$;

-- Products ------------------------------------------------------------------

INSERT INTO shop.products (sku, name, category, price, stock, is_active, created_at) VALUES
    ('EL-1001', 'Aurora Wireless Earbuds',           'Electronics',       79.90, 140, true,  now() - interval '900 days'),
    ('EL-1002', 'Volt 20W USB-C Charger',            'Electronics',       19.90, 420, true,  now() - interval '900 days'),
    ('EL-1003', 'Lumen 27" 4K Monitor',              'Electronics',      349.00,  35, true,  now() - interval '850 days'),
    ('EL-1004', 'Glide Mechanical Keyboard',         'Electronics',      119.00,  80, true,  now() - interval '800 days'),
    ('EL-1005', 'Orbit Smart Speaker',               'Electronics',       59.00,   0, false, now() - interval '900 days'),
    ('EL-1006', 'Pulse Fitness Tracker',             'Electronics',       89.00, 110, true,  now() - interval '600 days'),
    ('HK-2001', 'Nordic Ceramic Mug Set (4)',        'Home & Kitchen',    32.50, 260, true,  now() - interval '900 days'),
    ('HK-2002', 'Brewmaster Pour-Over Kettle',       'Home & Kitchen',    54.00,  95, true,  now() - interval '880 days'),
    ('HK-2003', 'Cast Iron Skillet 28cm',            'Home & Kitchen',    42.00, 120, true,  now() - interval '870 days'),
    ('HK-2004', 'Linen Tablecloth, Sand',            'Home & Kitchen',    38.00,  60, true,  now() - interval '700 days'),
    ('HK-2005', 'Bamboo Cutting Board',              'Home & Kitchen',    24.90, 300, true,  now() - interval '900 days'),
    ('HK-2006', 'Aroma Diffuser',                    'Home & Kitchen',    29.00,   0, false, now() - interval '890 days'),
    ('BK-3001', 'The Quiet Algorithm (Paperback)',   'Books',             14.99, 500, true,  now() - interval '900 days'),
    ('BK-3002', 'Practical PostgreSQL Patterns',     'Books',             39.00, 150, true,  now() - interval '820 days'),
    ('BK-3003', 'Atlas of Small Islands',            'Books',             27.50,  70, true,  now() - interval '760 days'),
    ('BK-3004', 'Cooking With Five Ingredients',     'Books',             22.00, 210, true,  now() - interval '900 days'),
    ('BK-3005', 'A Short History of Maps',           'Books',             18.00,  90, true,  now() - interval '650 days'),
    ('BK-3006', 'Night Trains of Europe',            'Books',             16.50, 130, true,  now() - interval '580 days'),
    ('SP-4001', 'Trailrunner Hydration Vest',        'Sports & Outdoors', 74.00,  55, true,  now() - interval '900 days'),
    ('SP-4002', 'Summit 30L Daypack',                'Sports & Outdoors', 89.00,  70, true,  now() - interval '860 days'),
    ('SP-4003', 'Cork Yoga Mat',                     'Sports & Outdoors', 49.00, 180, true,  now() - interval '900 days'),
    ('SP-4004', 'Insulated Steel Bottle 750ml',      'Sports & Outdoors', 26.00, 400, true,  now() - interval '900 days'),
    ('SP-4005', 'Ultralight Camping Hammock',        'Sports & Outdoors', 64.00,  40, true,  now() - interval '720 days'),
    ('SP-4006', 'Adjustable Dumbbells (pair)',       'Sports & Outdoors',199.00,  25, true,  now() - interval '690 days'),
    ('CL-5001', 'Merino Crew Socks (3 pack)',        'Clothing',          24.00, 350, true,  now() - interval '900 days'),
    ('CL-5002', 'Everyday Organic Cotton T-Shirt',   'Clothing',          19.00, 600, true,  now() - interval '900 days'),
    ('CL-5003', 'Packable Rain Jacket',              'Clothing',          99.00,  85, true,  now() - interval '840 days'),
    ('CL-5004', 'Wool Beanie, Charcoal',             'Clothing',          21.00, 230, true,  now() - interval '800 days'),
    ('CL-5005', 'Canvas Weekender Bag',              'Clothing',          79.00,  45, true,  now() - interval '760 days'),
    ('CL-5006', 'Fleece Half-Zip',                   'Clothing',          59.00,   0, false, now() - interval '900 days');

-- Customers -----------------------------------------------------------------

INSERT INTO shop.customers (email, full_name, country, city, created_at)
SELECT
    lower(fn || '.' || ln || g) || '@example.com',
    fn || ' ' || ln,
    split_part(place, ':', 1),
    split_part(place, ':', 2),
    date_trunc('minute', now() - interval '560 days' - pg_temp.rnd('c-created-' || g) * interval '440 days')
FROM generate_series(1, 80) AS g,
LATERAL (SELECT
    (ARRAY['Anna','Ben','Clara','David','Elif','Felix','Grace','Hugo','Ines','Jonas',
           'Kasia','Liam','Maya','Noah','Olga','Pavel','Rosa','Sami','Tara','Viktor'])
        [1 + floor(pg_temp.rnd('c-first-' || g) * 20)::int] AS fn,
    (ARRAY['Novak','Schmidt','Rossi','Dubois','Kowalski','Jansen','Silva','Larsen','Horvat','Moreau',
           'Weber','Costa','Nowak','Berg','Ivanova','Fischer','Marin','Kaya','Lindqvist','Varga'])
        [1 + floor(pg_temp.rnd('c-last-' || g) * 20)::int] AS ln,
    (ARRAY['PL:Wroclaw','PL:Krakow','PL:Warsaw','DE:Berlin','DE:Munich','NL:Amsterdam',
           'FR:Lyon','ES:Valencia','IT:Milan','SE:Stockholm','CZ:Prague','PT:Porto'])
        [1 + floor(pg_temp.rnd('c-place-' || g) * 12)::int] AS place
) AS names
ORDER BY g;

-- Orders --------------------------------------------------------------------
-- 1500 orders over the last ~18 months, denser towards the present (the shop
-- is growing). Status depends on age: recent orders are still in progress,
-- older ones are mostly delivered with a few cancellations and refunds.

INSERT INTO shop.orders (customer_id, status, ordered_at, shipping_country)
SELECT customer_id, status, ordered_at, c.country
FROM (
    SELECT
        g,
        1 + floor(pg_temp.rnd('o-cust-' || g) * 80)::int AS customer_id,
        days_ago,
        date_trunc('minute', now() - days_ago * interval '1 day') AS ordered_at,
        CASE
            WHEN days_ago < 3 THEN
                CASE WHEN s < 0.40 THEN 'pending' WHEN s < 0.92 THEN 'paid' ELSE 'cancelled' END
            WHEN days_ago < 14 THEN
                CASE WHEN s < 0.55 THEN 'shipped' WHEN s < 0.92 THEN 'delivered' ELSE 'cancelled' END
            ELSE
                CASE WHEN s < 0.90 THEN 'delivered' WHEN s < 0.96 THEN 'cancelled' ELSE 'refunded' END
        END AS status
    FROM generate_series(1, 1500) AS g,
    LATERAL (SELECT
        540 * power(pg_temp.rnd('o-date-' || g), 1.3) AS days_ago,
        pg_temp.rnd('o-status-' || g) AS s
    ) AS r
) AS o
JOIN shop.customers AS c ON c.id = o.customer_id
ORDER BY ordered_at;  -- so order ids increase with time, like a real shop

-- Order items ---------------------------------------------------------------
-- 1-4 lines per order. Product choice is skewed so a few products are clear
-- bestsellers; multiplying by 7 (coprime with 30) spreads the bestsellers
-- across categories. Orders older than a year were priced 5% lower.

INSERT INTO shop.order_items (order_id, product_id, quantity, unit_price)
SELECT
    o.id,
    p.id,
    1 + floor(power(pg_temp.rnd('i-qty-' || o.id || '-' || line), 3) * 4)::int,
    CASE WHEN o.ordered_at < now() - interval '365 days'
         THEN round(p.price * 0.95, 2)
         ELSE p.price
    END
FROM shop.orders AS o,
LATERAL generate_series(1, 1 + floor(pg_temp.rnd('i-lines-' || o.id) * 4)::int) AS line,
LATERAL (SELECT 1 + (floor(30 * power(pg_temp.rnd('i-prod-' || o.id || '-' || line), 1.8))::int * 7) % 30 AS product_id) AS pick
JOIN shop.products AS p ON p.id = pick.product_id
ORDER BY o.id, line
ON CONFLICT (order_id, product_id) DO NOTHING;

ANALYZE;
