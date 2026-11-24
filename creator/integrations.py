"""Lazy source-backed services; constructing a service never probes a provider."""
from __future__ import annotations

import asyncio

from creator.piarflow import PiarFlowClient, PiarFlowService
from creator.tgrass import TGrassClient, TGrassService


def ensure_services(ctx) -> None:
    from creator.integration_keys import value
    for name, table, enabled, client, service, token, url in (
        ('tgrass', 'tgrass_bots', ctx.cfg.tgrass_enabled, TGrassClient, TGrassService,
         value(ctx.cfg, "tgrass_factory"), ctx.cfg.tgrass_base_url),
        ('piarflow', 'pf_bots', ctx.cfg.piarflow_enabled, PiarFlowClient, PiarFlowService,
         value(ctx.cfg, "piarflow"), ctx.cfg.piarflow_base_url),
    ):
        # Existing recorded keys remain usable and accounted after removing a source.
        recorded = ctx.db.scalar(f"SELECT 1 FROM {table} WHERE api_key IS NOT NULL AND api_key<>'' "
                                 "AND status IN ('approved','disabled') LIMIT 1")
        if getattr(ctx, name) is None and (enabled or recorded):
            setattr(ctx, name, service(ctx, client(token, url)))


async def wait_and_sync(ctx, name: str) -> None:
    # Same existing sync loop; no sync/provider call is made by the settings handler.
    while getattr(ctx, name) is None:
        await asyncio.sleep(30)
    await getattr(ctx, name).sync_loop()
