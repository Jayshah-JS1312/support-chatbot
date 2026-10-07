-- Frozen trust-study seed. These are development/test identities and
-- business records only; example.com addresses cannot receive real email.
-- Keep this seed additive: never reset a tester's password or overwrite
-- an order whose state may already have changed in an existing environment.

begin;

insert into public.profiles (id, email, display_name, role, password_hash)
values
  ('55555555-5555-4555-8555-555555555555', 'priya@example.com', 'Priya', 'customer',
   '$2b$12$kFBCjdh114TzvKfcV8hbc.UryTO98UG9xL8zJza2g1Ajhr2sFksPC'),
  ('66666666-6666-4666-8666-666666666666', 'noah@example.com', 'Noah', 'customer',
   '$2b$12$7hbS6gDmRBmPJEXtFyvKLe5k1erJUeilJjhnZJxX05bSR0ueAHAJu')
on conflict do nothing;

insert into public.orders
  (order_number, user_id, item_name, price, status, ordered_on,
   delivered_on, estimated_delivery, carrier, version)
values
  -- Raj: the original delivered and shipped orders remain in migration 002.
  ('114-1000001-0000001', '11111111-1111-4111-8111-111111111111',
   'Bose SoundLink Speaker', 129.00, 'preparing', current_date - 1,
   null, current_date + 3, null, 1),
  ('114-1000002-0000002', '11111111-1111-4111-8111-111111111111',
   'Adjustable Monitor Stand', 54.99, 'cancelled', current_date - 8,
   null, null, null, 2),
  ('114-1000003-0000003', '11111111-1111-4111-8111-111111111111',
   'Countertop Blender', 79.99, 'return started', current_date - 14,
   current_date - 8, null, 'UPS', 2),
  ('114-1000004-0000004', '11111111-1111-4111-8111-111111111111',
   'Smartwatch Sport Band', 24.99, 'returned', current_date - 20,
   current_date - 15, null, 'USPS', 3),
  ('114-1000005-0000005', '11111111-1111-4111-8111-111111111111',
   'Camera Backpack', 89.00, 'delivered', current_date - 55,
   current_date - 48, null, 'UPS', 1),
  ('114-1000006-0000006', '11111111-1111-4111-8111-111111111111',
   'Electric Gooseneck Kettle', 74.99, 'shipped', current_date - 3,
   null, current_date + 2, 'Amazon Logistics', 1),

  -- Priya: delivery, refund/cancellation, and fulfillment scenarios.
  ('114-2000001-0000001', (select id from public.profiles where email = 'priya@example.com'),
   'Kindle Fabric Cover', 39.99, 'preparing', current_date,
   null, current_date + 4, null, 1),
  ('114-2000002-0000002', (select id from public.profiles where email = 'priya@example.com'),
   'Espresso Machine', 229.00, 'shipped', current_date - 3,
   null, current_date + 2, 'Amazon Logistics', 1),
  ('114-2000003-0000003', (select id from public.profiles where email = 'priya@example.com'),
   'Robot Vacuum', 499.99, 'delivered', current_date - 12,
   current_date - 6, null, 'UPS', 1),
  ('114-2000004-0000004', (select id from public.profiles where email = 'priya@example.com'),
   'Gaming Keyboard', 109.99, 'delivered', current_date - 4,
   current_date - 1, null, 'Amazon Logistics', 1),
  ('114-2000005-0000005', (select id from public.profiles where email = 'priya@example.com'),
   'Standing Desk Converter', 189.99, 'cancelled', current_date - 10,
   null, null, null, 2),
  ('114-2000006-0000006', (select id from public.profiles where email = 'priya@example.com'),
   'Digital Air Fryer', 119.99, 'return started', current_date - 18,
   current_date - 12, null, 'UPS', 2),
  ('114-2000007-0000007', (select id from public.profiles where email = 'priya@example.com'),
   'Fitness Tracker', 79.99, 'returned', current_date - 25,
   current_date - 20, null, 'USPS', 3),

  -- Noah: ambiguous products, return boundaries, and isolation scenarios.
  ('114-3000001-0000001', (select id from public.profiles where email = 'noah@example.com'),
   'Noise-Cancelling Earbuds', 159.99, 'preparing', current_date - 1,
   null, current_date + 3, null, 1),
  ('114-3000002-0000002', (select id from public.profiles where email = 'noah@example.com'),
   'USB-C Cable 1 m', 12.99, 'shipped', current_date - 2,
   null, current_date + 1, 'Amazon Logistics', 1),
  ('114-3000003-0000003', (select id from public.profiles where email = 'noah@example.com'),
   'USB-C Cable 2 m', 16.99, 'delivered', current_date - 9,
   current_date - 4, null, 'UPS', 1),
  ('114-3000004-0000004', (select id from public.profiles where email = 'noah@example.com'),
   'Rugged Phone Case', 34.99, 'delivered', current_date - 50,
   current_date - 43, null, 'USPS', 1),
  ('114-3000005-0000005', (select id from public.profiles where email = 'noah@example.com'),
   'HD Webcam', 69.99, 'cancelled', current_date - 14,
   null, null, null, 2),
  ('114-3000006-0000006', (select id from public.profiles where email = 'noah@example.com'),
   'Mechanical Keyboard', 139.99, 'return started', current_date - 16,
   current_date - 10, null, 'UPS', 2),
  ('114-3000007-0000007', (select id from public.profiles where email = 'noah@example.com'),
   'Portable SSD 1TB', 99.99, 'returned', current_date - 24,
   current_date - 18, null, 'UPS', 3)
on conflict (order_number) do nothing;

insert into public.order_events (order_id, event_type, detail, occurred_at, metadata)
select o.id, event.event_type, event.detail, event.occurred_at,
       jsonb_build_object('fixture', 'trust-study-v1')
from (values
  ('114-1000001-0000001', 'placed', 'Order placed and payment authorized', now() - interval '1 day'),
  ('114-1000002-0000002', 'cancelled', 'Cancellation completed; refund sent to original payment method', now() - interval '7 days'),
  ('114-1000003-0000003', 'return_started', 'Return label issued; carrier scan pending', now() - interval '1 day'),
  ('114-1000004-0000004', 'returned', 'Return received and refund completed', now() - interval '8 days'),
  ('114-1000005-0000005', 'delivered', 'Delivered at front door', now() - interval '48 days'),
  ('114-1000006-0000006', 'shipped', 'Package departed Amazon facility', now() - interval '1 day'),
  ('114-2000001-0000001', 'placed', 'Order placed and awaiting fulfillment', now()),
  ('114-2000002-0000002', 'shipped', 'Departed fulfillment center in Mississauga, ON', now() - interval '1 day'),
  ('114-2000003-0000003', 'delivered', 'Delivered and signed for by customer', now() - interval '6 days'),
  ('114-2000004-0000004', 'delivered', 'Marked delivered at front entrance', now() - interval '1 day'),
  ('114-2000005-0000005', 'cancelled', 'Cancellation completed; refund processing', now() - interval '9 days'),
  ('114-2000006-0000006', 'return_started', 'Return package accepted by UPS', now() - interval '2 days'),
  ('114-2000007-0000007', 'returned', 'Return received and refund completed', now() - interval '10 days'),
  ('114-3000001-0000001', 'placed', 'Order placed and payment authorized', now() - interval '1 day'),
  ('114-3000002-0000002', 'shipped', 'Package departed Amazon facility', now() - interval '1 day'),
  ('114-3000003-0000003', 'delivered', 'Delivered to parcel locker', now() - interval '4 days'),
  ('114-3000004-0000004', 'delivered', 'Delivered at front door', now() - interval '43 days'),
  ('114-3000005-0000005', 'cancelled', 'Cancellation completed before shipment', now() - interval '13 days'),
  ('114-3000006-0000006', 'return_started', 'Return label issued; carrier scan pending', now() - interval '1 day'),
  ('114-3000007-0000007', 'returned', 'Return received and refund completed', now() - interval '9 days')
) as event(order_number, event_type, detail, occurred_at)
join public.orders o on o.order_number = event.order_number
where not exists (
  select 1 from public.order_events existing
  where existing.order_id = o.id
    and existing.event_type = event.event_type
    and existing.detail = event.detail
);

commit;
