"""Bexio REST API client for API communication."""

import re
import time

import httpx
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field


class BexioConfig(BaseModel):
    """Configuration for Bexio connection."""

    api_url: str = Field(default="https://api.bexio.com", description="Bexio API base URL (without version)")
    access_token: str = Field(..., description="Bexio OAuth access token")
    timeout: int = Field(120, description="Request timeout in seconds")


class BexioClient:
    """Client for interacting with Bexio via REST API."""

    def __init__(self, config: BexioConfig) -> None:
        """Initialize Bexio client with configuration."""
        self.config = config
        self.api_url = config.api_url.rstrip("/")
        self.access_token = config.access_token
        self.timeout = config.timeout

        # Initialize HTTP client
        self.client = httpx.AsyncClient(
            timeout=self.timeout,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "bexio-mcp-server/0.1.0 (+https://github.com/tomasbottlik/bexio-mcp-server)",
            }
        )

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.client.aclose()

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Union[Dict[str, Any], List[Any]]] = None,
    ) -> Any:
        """Make a request to the Bexio API."""
        # Build URL without dropping the version path (avoid urljoin resetting path)
        base = self.api_url.rstrip('/')
        path = endpoint if endpoint.startswith('/') else f'/{endpoint}'
        url = f"{base}{path}"
        
        try:
            response = await self.client.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {"success": True}
            try:
                return response.json()
            except Exception:
                return response.text
        except httpx.HTTPStatusError as e:
            error_detail = f"HTTP {e.response.status_code}"
            try:
                error_data = e.response.json()
                # Prefer message if present
                message = error_data.get("message") or error_data.get("error") or error_data.get("detail")
                if message:
                    error_detail += f": {message}"
                # If there are field-level errors, include them for clarity
                field_errors = error_data.get("errors")
                if field_errors:
                    error_detail += f" | errors: {field_errors}"
            except Exception:
                # Fallback to raw text
                error_detail += f": {e.response.text}"
            raise ValueError(f"Bexio API error - {error_detail}")
        except Exception as e:
            raise ValueError(f"Request failed: {str(e)}")

    def _filter_by_criteria(
        self,
        items: List[Dict[str, Any]],
        criteria: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Filter a list of dicts according to bexio-like criteria.

        Supports minimal subset: criteria in {"=", "like"}. Compares as strings when needed.
        """
        def matches(item: Dict[str, Any]) -> bool:
            for cond in criteria:
                field = cond.get("field")
                value = cond.get("value")
                op = (cond.get("criteria") or "=").lower()
                if not field:
                    return False
                actual = item
                for part in field.split('.'):
                    if isinstance(actual, dict):
                        actual = actual.get(part)
                    else:
                        actual = None
                        break
                if op == "=":
                    if str(actual) != str(value):
                        return False
                elif op == "like":
                    if value is None:
                        return False
                    if str(value).lower() not in str(actual).lower():
                        return False
                else:
                    # Unknown operator: fail safe (exclude)
                    return False
            return True

        return [it for it in items if matches(it)]

    def _normalize_sales_document_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize quote/order/invoice payload aliases to API field names."""
        normalized = dict(payload)
        if "nr" in normalized and "document_nr" not in normalized:
            normalized["document_nr"] = normalized.pop("nr")
        if "project_id" in normalized and "pr_project_id" not in normalized:
            normalized["pr_project_id"] = normalized.pop("project_id")
        return normalized

    def _normalize_project_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize project payload aliases to API field names."""
        normalized = dict(payload)
        if "nr" in normalized and "document_nr" not in normalized:
            normalized["document_nr"] = normalized.pop("nr")
        return normalized

    def _normalize_item_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize legacy item aliases to API field names."""
        normalized = dict(payload)
        if "nr" in normalized and "intern_code" not in normalized:
            normalized["intern_code"] = normalized.pop("nr")
        alias_map = {
            "stock_min": "stock_min_nr",
            "stock_reserved": "stock_reserved_nr",
            "stock_available": "stock_available_nr",
            "stock_picked": "stock_picked_nr",
            "stock_disposed": "stock_disposed_nr",
            "stock_ordered": "stock_ordered_nr",
        }
        for legacy_key, api_key in alias_map.items():
            if legacy_key in normalized and api_key not in normalized:
                normalized[api_key] = normalized.pop(legacy_key)
        return normalized

    def _normalize_timesheet_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize timesheet payload to documented tracking shape."""
        normalized = dict(payload)
        tracking = normalized.get("tracking")
        tracking_type = normalized.pop("tracking_type", None)
        if tracking is None:
            date = normalized.pop("date", None)
            duration = normalized.pop("duration", None)
            start_time = normalized.pop("start_time", None)
            end_time = normalized.pop("end_time", None)
            if date and duration:
                normalized["tracking"] = {"type": "duration", "date": date, "duration": duration}
            elif date and start_time and end_time:
                normalized["tracking"] = {
                    "type": "range",
                    "date": date,
                    "start_time": start_time,
                    "end_time": end_time,
                }
            elif tracking_type in ("duration", "range"):
                # Keep user intent even when details are incomplete; API will validate remaining fields.
                normalized["tracking"] = {"type": tracking_type}
        if "allowable_bill" not in normalized:
            normalized["allowable_bill"] = False
        return normalized

    def _find_item_by_id(
        self,
        items: List[Dict[str, Any]],
        item_id: int,
        resource_name: str,
    ) -> Dict[str, Any]:
        """Find an object by id inside a list endpoint response."""
        for item in items:
            if str(item.get("id")) == str(item_id):
                return item
        raise ValueError(f"{resource_name} with id {item_id} not found")

    async def get(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Make a GET request."""
        return await self._request("GET", endpoint, params=params)

    async def post(
        self,
        endpoint: str,
        data: Union[Dict[str, Any], List[Any]],
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Make a POST request."""
        return await self._request("POST", endpoint, params=params, json_data=data)

    async def put(
        self,
        endpoint: str,
        data: Union[Dict[str, Any], List[Any]],
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Make a PUT request."""
        return await self._request("PUT", endpoint, params=params, json_data=data)

    async def delete(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Make a DELETE request."""
        return await self._request("DELETE", endpoint, params=params)

    # Contact methods
    async def list_contacts(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
        show_archived: bool = False,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of contacts.

        Args:
            limit: Maximum number of results to return
            offset: Number of results to skip
            order_by: Field to order results by
            show_archived: If True, include archived contacts in results
        """
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        if show_archived:
            params["show_archived"] = True

        return await self.get("/2.0/contact", params=params)

    async def get_contact(self, contact_id: int) -> Dict[str, Any]:
        """Fetch a specific contact."""
        return await self.get(f"/2.0/contact/{contact_id}")

    async def create_contact(self, contact_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new contact."""
        normalized: Dict[str, Any] = dict(contact_data)
        # Map common alias 'email' -> 'mail' expected by Bexio
        if "email" in normalized and "mail" not in normalized:
            normalized["mail"] = normalized.pop("email")
        return await self.post("/2.0/contact", normalized)

    async def update_contact(
        self, contact_id: int, contact_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update an existing contact."""
        normalized: Dict[str, Any] = dict(contact_data)
        if "email" in normalized and "mail" not in normalized:
            normalized["mail"] = normalized.pop("email")
        allowed_fields = {
            "nr",
            "contact_type_id",
            "name_1",
            "name_2",
            "salutation_id",
            "salutation_form",
            "title_id",
            "birthday",
            "address",
            "street_name",
            "house_number",
            "address_addition",
            "postcode",
            "city",
            "country_id",
            "mail",
            "mail_second",
            "phone_fixed",
            "phone_fixed_second",
            "phone_mobile",
            "fax",
            "url",
            "skype_name",
            "remarks",
            "language_id",
            "contact_group_ids",
            "contact_branch_ids",
            "user_id",
            "owner_id",
        }
        normalized = {k: v for k, v in normalized.items() if k in allowed_fields}
        # Merge with existing contact to satisfy required fields on update.
        try:
            existing = await self.get_contact(contact_id)
            existing_filtered = {k: v for k, v in existing.items() if k in allowed_fields}
            merged: Dict[str, Any] = {**existing_filtered, **normalized}
            return await self.post(f"/2.0/contact/{contact_id}", merged)
        except Exception:
            # Fallback: attempt update with provided fields only
            return await self.post(f"/2.0/contact/{contact_id}", normalized)

    async def delete_contact(self, contact_id: int) -> None:
        """Delete a contact."""
        await self.delete(f"/2.0/contact/{contact_id}")

    async def search_contacts(
        self,
        criteria: List[Dict[str, Any]],
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
        show_archived: bool = False,
    ) -> List[Dict[str, Any]]:
        """Search contacts with criteria."""
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        if show_archived:
            params["show_archived"] = True
        return await self.post("/2.0/contact/search", criteria, params=params)

    # Invoice methods
    async def list_invoices(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of invoices."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        
        return await self.get("/2.0/kb_invoice", params=params)

    async def get_invoice(self, invoice_id: int) -> Dict[str, Any]:
        """Fetch a specific invoice."""
        return await self.get(f"/2.0/kb_invoice/{invoice_id}")

    async def create_invoice(self, invoice_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new invoice."""
        normalized = self._normalize_sales_document_payload(invoice_data)
        # Basic validation to avoid opaque 422 errors
        if not normalized.get("contact_id"):
            raise ValueError("Invoice requires contact_id")
        positions = normalized.get("positions")
        if not positions or not isinstance(positions, list):
            raise ValueError(
                "Invoice requires at least one position. Provide positions=[{" 
                "\"type\": \"KbPositionCustom\", \"text\": \"Item description\", \"amount\": 1, \"unit_price\": 10.0}]"
            )
        return await self.post("/2.0/kb_invoice", normalized)

    async def update_invoice(
        self, invoice_id: int, invoice_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update an existing invoice."""
        normalized = self._normalize_sales_document_payload(invoice_data)
        return await self.post(f"/2.0/kb_invoice/{invoice_id}", normalized)

    async def delete_invoice(self, invoice_id: int) -> None:
        """Delete an invoice."""
        await self.delete(f"/2.0/kb_invoice/{invoice_id}")

    async def search_invoices(
        self,
        criteria: List[Dict[str, Any]],
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
        fallback_limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Search invoices with criteria.

        Tries API search; if it fails with validation (e.g., "field not set"), falls back to
        fetching a batch and filtering client-side using '=' and 'like'.
        """
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        try:
            return await self.post("/2.0/kb_invoice/search", criteria, params=params)
        except ValueError:
            # Fallback to client-side filtering
            batch = await self.list_invoices(limit=fallback_limit)
            return self._filter_by_criteria(batch, criteria)

    # Quote methods
    async def list_quotes(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of quotes."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        
        return await self.get("/2.0/kb_offer", params=params)

    async def get_quote(self, quote_id: int) -> Dict[str, Any]:
        """Fetch a specific quote."""
        return await self.get(f"/2.0/kb_offer/{quote_id}")

    async def create_quote(self, quote_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new quote."""
        normalized = self._normalize_sales_document_payload(quote_data)
        try:
            return await self.post("/2.0/kb_offer", normalized)
        except ValueError as e:
            error_msg = str(e)
            # If document_nr is required (automatic numbering disabled), generate one
            if "422" in error_msg and "document_nr" in error_msg and "document_nr" not in normalized:
                next_nr = await self._get_next_quote_document_nr()
                normalized["document_nr"] = next_nr
                return await self.post("/2.0/kb_offer", normalized)
            raise

    async def _get_next_quote_document_nr(self) -> str:
        """Generate the next quote document number based on existing quotes."""
        try:
            # Fetch recent quotes to find the highest document_nr
            quotes = await self.list_quotes(limit=100, order_by="id_desc")
            max_num = 0
            prefix = "AN-"  # Default prefix for Angebot (quote in German)

            for quote in quotes:
                doc_nr = quote.get("document_nr", "")
                if doc_nr:
                    # Extract prefix and number (e.g., "AN-00001" -> prefix="AN-", num=1)
                    match = re.match(r'^([A-Za-z]+-?)(\d+)$', doc_nr)
                    if match:
                        prefix = match.group(1)
                        num = int(match.group(2))
                        if num > max_num:
                            max_num = num

            # Generate next number with same prefix and padding
            next_num = max_num + 1
            return f"{prefix}{next_num:05d}"
        except Exception:
            # Fallback: use timestamp-based unique number
            return f"AN-{int(time.time())}"

    async def update_quote(
        self, quote_id: int, quote_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update an existing quote."""
        normalized = self._normalize_sales_document_payload(quote_data)
        return await self.post(f"/2.0/kb_offer/{quote_id}", normalized)

    async def delete_quote(self, quote_id: int) -> None:
        """Delete a quote."""
        await self.delete(f"/2.0/kb_offer/{quote_id}")

    async def search_quotes(
        self,
        criteria: List[Dict[str, Any]],
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
        fallback_limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Search quotes with criteria with robust fallbacks (see search_invoices)."""
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        try:
            return await self.post("/2.0/kb_offer/search", criteria, params=params)
        except ValueError:
            batch = await self.list_quotes(limit=fallback_limit)
            return self._filter_by_criteria(batch, criteria)

    # Order methods
    async def list_orders(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of orders."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        
        return await self.get("/2.0/kb_order", params=params)

    async def get_order(self, order_id: int) -> Dict[str, Any]:
        """Fetch a specific order."""
        return await self.get(f"/2.0/kb_order/{order_id}")

    async def create_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new order."""
        normalized = self._normalize_sales_document_payload(order_data)
        return await self.post("/2.0/kb_order", normalized)

    async def update_order(
        self, order_id: int, order_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update an existing order."""
        normalized = self._normalize_sales_document_payload(order_data)
        return await self.post(f"/2.0/kb_order/{order_id}", normalized)

    async def delete_order(self, order_id: int) -> None:
        """Delete an order."""
        await self.delete(f"/2.0/kb_order/{order_id}")

    async def search_orders(
        self,
        criteria: List[Dict[str, Any]],
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search orders with criteria."""
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        return await self.post("/2.0/kb_order/search", criteria, params=params)

    # Project methods
    async def list_projects(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of projects."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        
        return await self.get("/2.0/pr_project", params=params)

    async def get_project(self, project_id: int) -> Dict[str, Any]:
        """Fetch a specific project."""
        return await self.get(f"/2.0/pr_project/{project_id}")

    async def create_project(self, project_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new project."""
        normalized = self._normalize_project_payload(project_data)
        return await self.post("/2.0/pr_project", normalized)

    async def update_project(
        self, project_id: int, project_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update an existing project."""
        normalized = self._normalize_project_payload(project_data)
        return await self.post(f"/2.0/pr_project/{project_id}", normalized)

    async def delete_project(self, project_id: int) -> None:
        """Delete a project."""
        await self.delete(f"/2.0/pr_project/{project_id}")

    async def search_projects(
        self,
        criteria: List[Dict[str, Any]],
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search projects with criteria."""
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        return await self.post("/2.0/pr_project/search", criteria, params=params)

    # Item methods
    async def list_items(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of items."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        
        return await self.get("/2.0/article", params=params)

    async def get_item(self, item_id: int) -> Dict[str, Any]:
        """Fetch a specific item."""
        return await self.get(f"/2.0/article/{item_id}")

    async def create_item(self, item_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new item."""
        normalized = self._normalize_item_payload(item_data)
        return await self.post("/2.0/article", normalized)

    async def update_item(
        self, item_id: int, item_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update an existing item."""
        normalized = self._normalize_item_payload(item_data)
        return await self.post(f"/2.0/article/{item_id}", normalized)

    async def delete_item(self, item_id: int) -> None:
        """Delete an item."""
        await self.delete(f"/2.0/article/{item_id}")

    async def search_items(
        self,
        criteria: List[Dict[str, Any]],
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search items with criteria."""
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        return await self.post("/2.0/article/search", criteria, params=params)

    # ==================== ACCOUNTING METHODS ====================

    # Account methods (Chart of Accounts)
    async def list_accounts(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of accounts from the chart of accounts."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by

        return await self.get("/2.0/accounts", params=params)

    async def get_account(self, account_id: int) -> Dict[str, Any]:
        """Fetch a specific account.

        The API has no dedicated GET /2.0/accounts/{id} endpoint, so we resolve by listing and filtering.
        """
        accounts = await self.list_accounts(limit=2000)
        return self._find_item_by_id(accounts, account_id, "Account")

    async def search_accounts(
        self,
        criteria: List[Dict[str, Any]],
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Search accounts with criteria."""
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return await self.post("/2.0/accounts/search", criteria, params=params)

    # Account Group methods
    async def list_account_groups(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of account groups."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        return await self.get("/2.0/account_groups", params=params)

    async def get_account_group(self, account_group_id: int) -> Dict[str, Any]:
        """Fetch a specific account group.

        The API has no dedicated GET /2.0/account_groups/{id} endpoint, so we resolve by listing and filtering.
        """
        account_groups = await self.list_account_groups(limit=2000)
        return self._find_item_by_id(account_groups, account_group_id, "Account group")

    # Tax methods
    async def list_taxes(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        scope: Optional[str] = "active",
        date: Optional[str] = None,
        types: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of taxes.

        Args:
            limit: Maximum number of results to return
            offset: Number of results to skip
            scope: Filter by scope - 'active' (default) or None for all taxes including inactive
        """
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if scope is not None:
            params["scope"] = scope
        if date is not None:
            params["date"] = date
        if types is not None:
            params["types"] = types

        return await self.get("/3.0/taxes", params=params)

    async def get_tax(self, tax_id: int) -> Dict[str, Any]:
        """Fetch a specific tax."""
        return await self.get(f"/3.0/taxes/{tax_id}")

    # Currency methods
    async def list_currencies(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of currencies."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if date is not None:
            params["date"] = date

        return await self.get("/3.0/currencies", params=params)

    async def get_currency(self, currency_id: int) -> Dict[str, Any]:
        """Fetch a specific currency."""
        return await self.get(f"/3.0/currencies/{currency_id}")

    async def create_currency(self, currency_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new currency."""
        return await self.post("/3.0/currencies", currency_data)

    async def get_exchange_rates(self, currency_id: int, date: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch exchange rates for currencies."""
        params = {}
        if date is not None:
            params["date"] = date
        return await self.get(f"/3.0/currencies/{currency_id}/exchange_rates", params=params)

    # Manual Entry / Accounting Journal methods
    async def list_manual_entries(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of manual entries (accounting journal)."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by

        return await self.get("/3.0/accounting/manual_entries", params=params)

    async def get_manual_entry(self, entry_id: int) -> Dict[str, Any]:
        """Fetch a specific manual entry.

        The API has no dedicated GET /3.0/accounting/manual_entries/{id} endpoint, so we resolve by listing and filtering.
        """
        entries = await self.list_manual_entries(limit=2000)
        return self._find_item_by_id(entries, entry_id, "Manual entry")

    async def create_manual_entry(self, entry_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new manual entry (accounting journal booking).

        The payload structure requires:
        - type: Entry type (required), one of:
          - "manual_single_entry": Simple one-line booking (debit/credit/amount)
          - "manual_compound_entry": Complex booking with amount split across multiple accounts
          - "manual_group_entry": Multiple one-line bookings with same reference_nr
        - date: Booking date (YYYY-MM-DD format, required)
        - reference_nr: Reference number (optional, e.g., "Booking BA-22")
        - entries: Array of entry objects (required), each containing:
          - debit_account_id: Debit account ID (required for single/group, optional for compound)
          - credit_account_id: Credit account ID (required for single/group, optional for compound)
          - amount: Booking amount (required)
          - description: Entry description (optional, max 255 chars)
          - tax_id: Tax ID for VAT (optional)
          - tax_account_id: Account ID for tax (debit or credit account, optional)
          - currency_id: Currency ID (optional, defaults to base currency)
          - currency_factor: Exchange rate factor (optional, 1 if same as base currency)

        Example - manual_single_entry (simple booking):
        {
            "type": "manual_single_entry",
            "date": "2024-01-15",
            "reference_nr": "Payment-001",
            "entries": [{
                "debit_account_id": 1020,
                "credit_account_id": 3200,
                "amount": 1000.00,
                "description": "Customer payment received"
            }]
        }

        Example - manual_compound_entry (split across accounts):
        {
            "type": "manual_compound_entry",
            "date": "2024-01-15",
            "entries": [
                {"debit_account_id": 1020, "amount": 25000},
                {"credit_account_id": 3200, "amount": 10000},
                {"credit_account_id": 3201, "amount": 8000},
                {"credit_account_id": 3202, "amount": 7000}
            ]
        }

        Example - manual_group_entry (multiple bookings, same reference):
        {
            "type": "manual_group_entry",
            "date": "2024-01-15",
            "reference_nr": "Multi-001",
            "entries": [
                {"debit_account_id": 1020, "credit_account_id": 3200, "amount": 13600},
                {"debit_account_id": 1021, "credit_account_id": 3201, "amount": 7230}
            ]
        }
        """
        return await self.post("/3.0/accounting/manual_entries", entry_data)

    async def get_next_reference_number(self) -> Dict[str, Any]:
        """Get the next available reference number for manual entries."""
        return await self.get("/3.0/accounting/manual_entries/next_ref_nr")

    # Journal Report methods (read-only accounting journal/ledger)
    async def get_journal(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        account_uuid: Optional[str] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Fetch accounting journal entries (read-only ledger report).

        This endpoint returns the accounting journal/ledger with filtering options.
        Different from manual_entries which are for creating/managing journal entries.

        Args:
            from_date: Start date filter (YYYY-MM-DD format)
            to_date: End date filter (YYYY-MM-DD format)
            account_uuid: Filter by specific account UUID
            limit: Maximum number of results (default 500)
            offset: Number of results to skip (default 0)
        """
        params: Dict[str, Any] = {
            "limit": limit,
            "offset": offset,
        }
        if from_date is not None:
            params["from"] = from_date
        if to_date is not None:
            params["to"] = to_date
        if account_uuid is not None:
            params["account_uuid"] = account_uuid

        return await self.get("/3.0/accounting/journal", params=params)

    # Business Year methods
    async def list_business_years(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of business years."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        return await self.get("/3.0/accounting/business_years", params=params)

    async def get_business_year(self, business_year_id: int) -> Dict[str, Any]:
        """Fetch a specific business year."""
        return await self.get(f"/3.0/accounting/business_years/{business_year_id}")

    # Calendar Year methods
    async def list_calendar_years(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of calendar years."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        return await self.get("/3.0/accounting/calendar_years", params=params)

    async def get_calendar_year(self, calendar_year_id: int) -> Dict[str, Any]:
        """Fetch a specific calendar year."""
        return await self.get(f"/3.0/accounting/calendar_years/{calendar_year_id}")

    async def create_calendar_year(self, calendar_year_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new calendar year.

        Required fields:
        - start: Start date (YYYY-MM-DD format)
        - end: End date (YYYY-MM-DD format)
        """
        return await self.post("/3.0/accounting/calendar_years", calendar_year_data)

    async def search_calendar_years(self, criteria: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Search calendar years with criteria."""
        return await self.post("/3.0/accounting/calendar_years/search", criteria)

    # VAT Period methods
    async def list_vat_periods(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of VAT periods."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        return await self.get("/3.0/accounting/vat_periods", params=params)

    async def get_vat_period(self, vat_period_id: int) -> Dict[str, Any]:
        """Fetch a specific VAT period."""
        return await self.get(f"/3.0/accounting/vat_periods/{vat_period_id}")

    # ==================== TIMESHEET METHODS ====================

    # Timesheet methods
    async def list_timesheets(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of timesheets."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by

        return await self.get("/2.0/timesheet", params=params)

    async def get_timesheet(self, timesheet_id: int) -> Dict[str, Any]:
        """Fetch a specific timesheet."""
        return await self.get(f"/2.0/timesheet/{timesheet_id}")

    async def create_timesheet(self, timesheet_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new timesheet entry.

        Required fields:
        - user_id: User ID performing the work
        - client_service_id: Client service ID
        - allowable_bill: Whether the time is billable
        - tracking: Tracked time payload

        Optional fields:
        - date/duration: Legacy aliases converted to tracking automatically
        - allowable_bill: Whether the time is billable (boolean)
        - text: Description text
        - contact_id: Contact ID
        - pr_project_id: Project ID
        """
        normalized = self._normalize_timesheet_payload(timesheet_data)
        return await self.post("/2.0/timesheet", normalized)

    async def update_timesheet(
        self, timesheet_id: int, timesheet_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update an existing timesheet entry.

        Args:
            timesheet_id: ID of the timesheet to update
            timesheet_data: Updated timesheet data
        """
        normalized = self._normalize_timesheet_payload(timesheet_data)
        return await self.post(f"/2.0/timesheet/{timesheet_id}", normalized)

    async def delete_timesheet(self, timesheet_id: int) -> None:
        """Delete a timesheet entry.

        Args:
            timesheet_id: ID of the timesheet to delete
        """
        await self.delete(f"/2.0/timesheet/{timesheet_id}")

    async def search_timesheets(
        self,
        criteria: List[Dict[str, Any]],
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
        fallback_limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Search timesheets with criteria."""
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        try:
            return await self.post("/2.0/timesheet/search", criteria, params=params)
        except ValueError:
            batch = await self.list_timesheets(limit=fallback_limit)
            return self._filter_by_criteria(batch, criteria)

    # Timesheet Status methods
    async def list_timesheet_statuses(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of timesheet statuses."""
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        return await self.get("/2.0/timesheet_status", params=params)

    async def get_timesheet_status(self, status_id: int) -> Dict[str, Any]:
        """Fetch a specific timesheet status.

        The API has no dedicated GET /2.0/timesheet_status/{id} endpoint, so we resolve by listing and filtering.
        """
        statuses = await self.list_timesheet_statuses(limit=2000)
        return self._find_item_by_id(statuses, status_id, "Timesheet status")

    # Client Service methods
    async def list_client_services(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of client services."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by

        return await self.get("/2.0/client_service", params=params)

    async def get_client_service(self, client_service_id: int) -> Dict[str, Any]:
        """Fetch a specific client service.

        The API has no dedicated GET /2.0/client_service/{id} endpoint, so we resolve by listing and filtering.
        """
        services = await self.list_client_services(limit=2000)
        return self._find_item_by_id(services, client_service_id, "Client service")

    async def create_client_service(self, client_service_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new client service.

        Required fields:
        - name: Service name
        Optional fields:
        - default_is_billable
        - default_price_per_hour
        - account_id
        """
        return await self.post("/2.0/client_service", client_service_data)

    async def search_client_services(
        self,
        criteria: List[Dict[str, Any]],
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search client services with criteria."""
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        return await self.post("/2.0/client_service/search", criteria, params=params)

    # Business Activity methods
    async def list_business_activities(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of business activities (same as client services in Bexio API)."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by

        return await self.get("/2.0/client_service", params=params)

    async def get_business_activity(self, business_activity_id: int) -> Dict[str, Any]:
        """Fetch a specific business activity (same as client service in Bexio API)."""
        activities = await self.list_business_activities(limit=2000)
        return self._find_item_by_id(activities, business_activity_id, "Business activity")

    async def create_business_activity(self, business_activity_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new business activity (same as client service in Bexio API).

        Required fields:
        - name: Activity name
        """
        return await self.post("/2.0/client_service", business_activity_data)

    async def search_business_activities(
        self,
        criteria: List[Dict[str, Any]],
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search business activities with criteria (same as client services in Bexio API)."""
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        return await self.post("/2.0/client_service/search", criteria, params=params)
