-- Demo e-commerce schema. Everything lives in the `shop` schema so the
-- read-only role can be granted exactly this schema and nothing else.

CREATE SCHEMA shop;

CREATE TABLE shop.customers (
    id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email       text        NOT NULL UNIQUE,
    full_name   text        NOT NULL,
    country     text        NOT NULL,
    city        text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE shop.products (
    id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku         text          NOT NULL UNIQUE,
    name        text          NOT NULL,
    category    text          NOT NULL,
    price       numeric(10,2) NOT NULL CHECK (price > 0),
    stock       integer       NOT NULL DEFAULT 0 CHECK (stock >= 0),
    is_active   boolean       NOT NULL DEFAULT true,
    created_at  timestamptz   NOT NULL DEFAULT now()
);

CREATE TABLE shop.orders (
    id               integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id      integer     NOT NULL REFERENCES shop.customers(id),
    status           text        NOT NULL CHECK (status IN
                         ('pending', 'paid', 'shipped', 'delivered', 'cancelled', 'refunded')),
    ordered_at       timestamptz NOT NULL,
    shipping_country text        NOT NULL
);

CREATE TABLE shop.order_items (
    id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id    integer       NOT NULL REFERENCES shop.orders(id) ON DELETE CASCADE,
    product_id  integer       NOT NULL REFERENCES shop.products(id),
    quantity    integer       NOT NULL CHECK (quantity > 0),
    unit_price  numeric(10,2) NOT NULL CHECK (unit_price > 0),
    UNIQUE (order_id, product_id)
);

CREATE INDEX orders_customer_id_idx ON shop.orders (customer_id);
CREATE INDEX orders_ordered_at_idx  ON shop.orders (ordered_at);
CREATE INDEX order_items_product_id_idx ON shop.order_items (product_id);

-- Comments are returned by describe_table, so they double as documentation
-- for the agent: they tell it what each table and column means.
COMMENT ON TABLE shop.customers   IS 'People who have an account in the shop.';
COMMENT ON TABLE shop.products    IS 'Catalog of items for sale.';
COMMENT ON TABLE shop.orders      IS 'One row per checkout. Revenue only counts orders with status paid, shipped or delivered.';
COMMENT ON TABLE shop.order_items IS 'Line items of an order. Line total = quantity * unit_price.';

COMMENT ON COLUMN shop.customers.country      IS 'ISO 3166-1 alpha-2 country code.';
COMMENT ON COLUMN shop.products.sku           IS 'Stock keeping unit, unique product code.';
COMMENT ON COLUMN shop.products.price         IS 'Current list price in EUR.';
COMMENT ON COLUMN shop.products.is_active     IS 'False when the product is discontinued.';
COMMENT ON COLUMN shop.orders.status          IS 'pending, paid, shipped, delivered, cancelled or refunded.';
COMMENT ON COLUMN shop.orders.shipping_country IS 'ISO 3166-1 alpha-2 country code.';
COMMENT ON COLUMN shop.order_items.unit_price IS 'Price in EUR at the time of purchase (may differ from current product price).';
