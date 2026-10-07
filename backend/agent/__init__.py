"""Agent runtime services.

``events/`` holds the delivery substrate (ledger, outbox, consumer). This
package holds the agent services that ride on it. The first is the model
gateway, because every other service needs a recorded, validated way to call a
model before it is allowed to act.

Nothing in here may reach the database without a tenant bound; nothing may
treat a model response as fact without validation.
"""
