"""Typed results returned by the tools.

The MCP SDK turns each tool's return annotation into an ``output_schema``,
validates the returned object against it, and sends it to the client twice:
as JSON text for the model, and as ``structured_content`` for applications.
Money is ``Decimal`` (serialised as a string such as "79.90"), never float.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

OrderStatus = Literal["pending", "paid", "shipped", "delivered", "cancelled", "refunded"]
Period = Literal["last_7_days", "last_30_days", "last_90_days", "last_365_days", "all_time"]


# Schema discovery ----------------------------------------------------------


class TableSummary(BaseModel):
    name: str
    description: str | None
    approximate_rows: int = Field(description="Planner estimate, not an exact count.")


class TableList(BaseModel):
    tables: list[TableSummary]


class Column(BaseModel):
    name: str
    type: str
    nullable: bool
    default: str | None
    primary_key: bool
    description: str | None


class ForeignKey(BaseModel):
    column: str
    references_table: str
    references_column: str


class IncomingReference(BaseModel):
    table: str
    column: str
    references_column: str


class TableDescription(BaseModel):
    name: str
    description: str | None
    columns: list[Column]
    foreign_keys: list[ForeignKey] = Field(description="Columns of this table that point to other tables.")
    referenced_by: list[IncomingReference] = Field(description="Columns of other tables that point to this one.")


# Customers and orders ------------------------------------------------------


class Customer(BaseModel):
    id: int
    full_name: str
    email: str
    country: str
    city: str
    created_at: datetime


class CustomerSearch(BaseModel):
    customers: list[Customer]
    truncated: bool = Field(description="True if more customers matched than were returned.")


class OrderItem(BaseModel):
    product_id: int
    product_name: str
    quantity: int
    unit_price: Decimal


class Order(BaseModel):
    order_id: int
    ordered_at: datetime
    status: OrderStatus
    total: Decimal
    items: list[OrderItem]


class CustomerOrders(BaseModel):
    customer: Customer
    order_count: int = Field(description="All orders of this customer, in any status.")
    lifetime_value: Decimal = Field(description="Total of paid, shipped and delivered orders, in EUR.")
    orders: list[Order] = Field(description="Most recent first.")
    truncated: bool = Field(description="True if the customer has more matching orders than were returned.")


# Sales ---------------------------------------------------------------------


class ProductSales(BaseModel):
    rank: int
    product_id: int
    sku: str
    name: str
    category: str
    units_sold: int
    revenue: Decimal


class TopProducts(BaseModel):
    period: Period
    since: datetime | None = Field(description="Start of the period (UTC); null for all_time.")
    ranked_by: Literal["revenue", "units"]
    category: str | None
    products: list[ProductSales]


class RevenuePeriod(BaseModel):
    period_start: date
    revenue: Decimal
    orders: int


class RevenueSummary(BaseModel):
    start_date: date
    end_date: date
    currency: Literal["EUR"] = "EUR"
    revenue: Decimal = Field(description="Sum of paid, shipped and delivered orders.")
    orders: int = Field(description="Number of paid, shipped and delivered orders.")
    average_order_value: Decimal | None
    customers: int = Field(description="Distinct customers with at least one counted order.")
    cancelled_orders: int
    refunded_orders: int
    group_by: Literal["none", "day", "week", "month", "quarter"]
    breakdown: list[RevenuePeriod]
    breakdown_truncated: bool


# Free-form SQL -------------------------------------------------------------


class QueryRows(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool = Field(description="True if the query returned more rows than the row limit.")
