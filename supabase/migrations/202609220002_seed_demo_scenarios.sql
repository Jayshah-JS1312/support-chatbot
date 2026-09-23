insert into public.profiles (id, email, display_name)
values
  ('11111111-1111-4111-8111-111111111111', 'raj@example.com', 'Raj'),
  ('22222222-2222-4222-8222-222222222222', 'mei@example.com', 'Mei'),
  ('33333333-3333-4333-8333-333333333333', 'alex@example.com', 'Alex'),
  ('44444444-4444-4444-8444-444444444444', 'sam@example.com', 'Sam')
on conflict (email) do nothing;

insert into public.orders
  (order_number, profile_id, item_name, price, status, ordered_on,
   delivered_on, estimated_delivery, carrier)
values
  ('112-1111111-1111111', '11111111-1111-4111-8111-111111111111',
   'Sony WH-1000XM5 Headphones', 348.00, 'delivered', current_date - 9,
   current_date - 4, null, 'UPS'),
  ('112-2222222-2222222', '11111111-1111-4111-8111-111111111111',
   'Instant Pot Duo 6qt', 89.99, 'shipped', current_date - 2,
   null, current_date + 1, 'Amazon Logistics'),
  ('112-3333333-3333333', '22222222-2222-4222-8222-222222222222',
   'Kindle Paperwhite 16GB', 149.99, 'preparing', current_date,
   null, current_date + 4, null),
  ('112-4444444-4444444', '22222222-2222-4222-8222-222222222222',
   'Logitech MX Master 3S', 99.99, 'delivered', current_date - 70,
   current_date - 64, null, 'USPS'),
  ('112-5555555-5555555', '33333333-3333-4333-8333-333333333333',
   'USB-C Cable', 14.99, 'delivered', current_date - 8,
   current_date - 3, null, 'UPS'),
  ('112-6666666-6666666', '44444444-4444-4444-8444-444444444444',
   'Echo Dot', 49.99, 'shipped', current_date - 5,
   null, current_date + 2, 'Amazon Logistics')
on conflict (order_number) do nothing;

insert into public.order_events (order_id, event_type, detail, occurred_at)
select o.id, event.event_type, event.detail, event.occurred_at
from (values
  ('112-1111111-1111111', 'shipped', 'Shipped from Newark, NJ', now() - interval '8 days'),
  ('112-1111111-1111111', 'in_transit', 'Arrived at facility, Columbus, OH', now() - interval '6 days'),
  ('112-1111111-1111111', 'delivered', 'Delivered — left at front door', now() - interval '4 days'),
  ('112-2222222-2222222', 'placed', 'Order placed', now() - interval '2 days'),
  ('112-2222222-2222222', 'shipped', 'Shipped from Edison, NJ', now() - interval '1 day'),
  ('112-3333333-3333333', 'placed', 'Order placed', now()),
  ('112-4444444-4444444', 'delivered', 'Delivered — handed to resident', now() - interval '64 days')
) as event(order_number, event_type, detail, occurred_at)
join public.orders o on o.order_number = event.order_number
where not exists (
  select 1 from public.order_events existing
  where existing.order_id = o.id and existing.event_type = event.event_type
    and existing.detail = event.detail
);
