---
merchant: Cascade Outfitters
merchant_id: cascade
rules:
  return:
    window_days: 30
    anchor: delivery_date
    excludes: [worn, final_sale]
  duplicate_order:
    window_days: 30
    anchor: delivery_date
---
# Cascade Outfitters - Returns and Exchanges

Effective August 15, 2026. Demo policy of a fictional retailer.

## Returns

Unworn items with original tags attached can be returned within 30 days of delivery for a full refund to the original payment method.

Final sale items cannot be returned.

## Duplicate Orders

If you accidentally placed the same order twice, you can return the duplicate within 30 days of delivery. We cover return shipping for duplicate orders and refund the full amount, including shipping.

## How to Start a Return

Start a return in the Cascade Outfitters returns portal with your order number. You will receive a prepaid label by email.
