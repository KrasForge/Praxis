"""Noesis noesis-kb-v1 REST adapter.

https://github.com/KrasForge/Noesis/blob/main/contracts/noesis-kb-v1.md
Freshness is the KB observation time (as_of_ms), not document publication time.
"""

import json
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from praxis.knowledge.context import ContextItem, ContextRequest, ContextResponse, bound_response
from praxis.transport.http import JSONTransport, TransportError


class NoesisContextProvider:
    def __init__(self, transport: JSONTransport):
        self.transport = transport

    async def query(self, request: ContextRequest) -> ContextResponse:
        filters = dict(request.filters)
        if set(filters) != {"domain"}:
            return ContextResponse("unavailable", reason="noesis_domain_filter_required")
        domain = filters["domain"]
        path = "/api/v1/kb/" + quote(domain, safe="") + "/search?" + urlencode({
            "q": request.query, "limit": request.max_results})
        try:
            reply = await self.transport.request("GET", path)
            if reply.get("contract") != "noesis-kb-v1" or reply.get("domain") != domain:
                raise ValueError("noesis_contract_mismatch")
            as_of = reply["as_of_ms"]
            if type(as_of) is not int or not isinstance(reply["data"], list):
                raise ValueError("invalid noesis response")
            observed = datetime.fromtimestamp(as_of / 1000, timezone.utc).isoformat()
            items = []
            for row in reply["data"]:
                identity = row.get("id", row.get("document_id"))
                if not isinstance(identity, str) or not identity:
                    raise ValueError("missing source identity")
                items.append(ContextItem(identity, json.dumps(row, ensure_ascii=False), observed,
                                         json.dumps({"noesis": {"contract": reply["contract"],
                                                    "domain": domain, "as_of_ms": as_of, "source": row}})))
            return bound_response(request, ContextResponse("available", tuple(items)))
        except TransportError as exc:
            return ContextResponse("unavailable", reason="noesis_" + exc.code)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError):
            return ContextResponse("unavailable", reason="noesis_invalid_response")
