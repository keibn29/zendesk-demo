"""Schemas package.

Webhook payloads are normalized by `app.api.payloads` into frozen dataclasses
(`SlackPayload`, `ZendeskTicketEvent`). They are deliberately not pydantic
models: an authenticated-but-malformed body must return a safe result, not
raise a validation error that would surface as a 500.
"""