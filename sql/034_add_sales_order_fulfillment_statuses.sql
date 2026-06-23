-- Adds fulfillment-tracking statuses to sales_orders.status. Once a payment is
-- confirmed ('paid'), the admin dashboard advances the order one step at a
-- time: initiated -> processed -> shipped -> out_for_delivery -> delivered,
-- or cancels it at any point along the way. Existing values are kept so
-- already-created orders (and the payment lifecycle: draft/pending_payment/
-- paid/failed) are unaffected.

alter table if exists public.sales_orders
  drop constraint if exists sales_orders_status_check;

alter table if exists public.sales_orders
  add constraint sales_orders_status_check
  check (status in (
    'draft', 'pending_payment', 'paid', 'failed', 'cancelled', 'fulfilled',
    'initiated', 'processed', 'shipped', 'out_for_delivery', 'delivered'
  ));
