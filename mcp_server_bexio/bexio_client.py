"""Bexio REST API client for API communication."""

import httpx
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urljoin

from pydantic import BaseModel, Field, ValidationError


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
        json_data: Optional[Dict[str, Any]] = None,
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
            return response.json()
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
        data: Dict[str, Any],
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Make a POST request."""
        return await self._request("POST", endpoint, params=params, json_data=data)

    async def put(
        self,
        endpoint: str,
        data: Dict[str, Any],
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
    ) -> List[Dict[str, Any]]:
        """Fetch a list of contacts."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if order_by is not None:
            params["order_by"] = order_by
        
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
        # Merge with existing contact to satisfy required fields on PUT
        try:
            existing = await self.get_contact(contact_id)
            merged: Dict[str, Any] = {**existing, **normalized}
            return await self.put(f"/2.0/contact/{contact_id}", merged)
        except Exception:
            # Fallback: attempt update with provided fields only
            return await self.put(f"/2.0/contact/{contact_id}", normalized)

    async def delete_contact(self, contact_id: int) -> None:
        """Delete a contact."""
        await self.delete(f"/2.0/contact/{contact_id}")

    async def search_contacts(self, criteria: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Search contacts with criteria."""
        return await self.post("/2.0/contact/search", criteria)

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
        # Basic validation to avoid opaque 422 errors
        if not invoice_data.get("contact_id"):
            raise ValueError("Invoice requires contact_id")
        positions = invoice_data.get("positions")
        if not positions or not isinstance(positions, list):
            raise ValueError(
                "Invoice requires at least one position. Provide positions=[{" 
                "\"type\": \"KbPositionCustom\", \"text\": \"Item description\", \"amount\": 1, \"unit_price\": 10.0}]"
            )
        return await self.post("/2.0/kb_invoice", invoice_data)

    async def update_invoice(
        self, invoice_id: int, invoice_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update an existing invoice."""
        return await self.put(f"/2.0/kb_invoice/{invoice_id}", invoice_data)

    async def delete_invoice(self, invoice_id: int) -> None:
        """Delete an invoice."""
        await self.delete(f"/2.0/kb_invoice/{invoice_id}")

    async def search_invoices(self, criteria: List[Dict[str, Any]], *, fallback_limit: int = 200) -> List[Dict[str, Any]]:
        """Search invoices with criteria.

        Tries API search; if it fails with validation (e.g., "field not set"), falls back to
        fetching a batch and filtering client-side using '=' and 'like'.
        """
        try:
            return await self.post("/2.0/kb_invoice/search", criteria)
        except ValueError as e:
            # Try alternate payload shape {"criteria": [...]} in case of schema variance
            try:
                return await self.post("/2.0/kb_invoice/search", {"criteria": criteria})
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
        return await self.post("/2.0/kb_offer", quote_data)

    async def update_quote(
        self, quote_id: int, quote_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update an existing quote."""
        return await self.put(f"/2.0/kb_offer/{quote_id}", quote_data)

    async def delete_quote(self, quote_id: int) -> None:
        """Delete a quote."""
        await self.delete(f"/2.0/kb_offer/{quote_id}")

    async def search_quotes(self, criteria: List[Dict[str, Any]], *, fallback_limit: int = 200) -> List[Dict[str, Any]]:
        """Search quotes with criteria with robust fallbacks (see search_invoices)."""
        try:
            return await self.post("/2.0/kb_offer/search", criteria)
        except ValueError:
            try:
                return await self.post("/2.0/kb_offer/search", {"criteria": criteria})
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
        return await self.post("/2.0/kb_order", order_data)

    async def update_order(
        self, order_id: int, order_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update an existing order."""
        return await self.put(f"/2.0/kb_order/{order_id}", order_data)

    async def delete_order(self, order_id: int) -> None:
        """Delete an order."""
        await self.delete(f"/2.0/kb_order/{order_id}")

    async def search_orders(self, criteria: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Search orders with criteria."""
        return await self.post("/2.0/kb_order/search", {"criteria": criteria})

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
        return await self.post("/2.0/pr_project", project_data)

    async def update_project(
        self, project_id: int, project_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update an existing project."""
        return await self.put(f"/2.0/pr_project/{project_id}", project_data)

    async def delete_project(self, project_id: int) -> None:
        """Delete a project."""
        await self.delete(f"/2.0/pr_project/{project_id}")

    async def search_projects(self, criteria: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Search projects with criteria."""
        return await self.post("/2.0/pr_project/search", {"criteria": criteria})

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
        return await self.post("/2.0/article", item_data)

    async def update_item(
        self, item_id: int, item_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update an existing item."""
        return await self.put(f"/2.0/article/{item_id}", item_data)

    async def delete_item(self, item_id: int) -> None:
        """Delete an item."""
        await self.delete(f"/2.0/article/{item_id}")

    async def search_items(self, criteria: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Search items with criteria."""
        return await self.post("/2.0/article/search", {"criteria": criteria})

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
        """Fetch a specific account."""
        return await self.get(f"/2.0/accounts/{account_id}")

    async def search_accounts(self, criteria: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Search accounts with criteria."""
        return await self.post("/2.0/accounts/search", criteria)

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
        """Fetch a specific account group."""
        return await self.get(f"/2.0/account_groups/{account_group_id}")

    # Tax methods
    async def list_taxes(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of taxes."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        return await self.get("/3.0/taxes", params=params)

    async def get_tax(self, tax_id: int) -> Dict[str, Any]:
        """Fetch a specific tax."""
        return await self.get(f"/3.0/taxes/{tax_id}")

    # Currency methods
    async def list_currencies(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of currencies."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        return await self.get("/3.0/currencies", params=params)

    async def get_currency(self, currency_id: int) -> Dict[str, Any]:
        """Fetch a specific currency."""
        return await self.get(f"/3.0/currencies/{currency_id}")

    async def create_currency(self, currency_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new currency."""
        return await self.post("/3.0/currencies", currency_data)

    async def get_exchange_rates(self, date: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch exchange rates for currencies."""
        params = {}
        if date is not None:
            params["date"] = date
        return await self.get("/3.0/currencies/exchange_rates", params=params)

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
        """Fetch a specific manual entry."""
        return await self.get(f"/3.0/accounting/manual_entries/{entry_id}")

    async def create_manual_entry(self, entry_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new manual entry (accounting journal booking).

        Required fields:
        - date: Booking date (YYYY-MM-DD format)
        - debit_account_id: Debit account ID
        - credit_account_id: Credit account ID
        - amount: Booking amount

        Optional fields:
        - tax_id: Tax ID for VAT
        - text: Description/reference text
        - reference_nr: Reference number
        """
        return await self.post("/3.0/accounting/manual_entries", entry_data)

    async def get_next_reference_number(self) -> Dict[str, Any]:
        """Get the next available reference number for manual entries."""
        return await self.get("/3.0/accounting/manual_entries/reference_number")

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
        - date: Date of the timesheet entry (YYYY-MM-DD)
        - duration: Duration in HH:MM format (e.g., "02:30")

        Optional fields:
        - allowable_bill: Whether the time is billable (boolean)
        - text: Description text
        - contact_id: Contact ID
        - pr_project_id: Project ID
        - tracking_type: Type of tracking (0 or 1)
        """
        return await self.post("/2.0/timesheet", timesheet_data)

    async def search_timesheets(
        self, criteria: List[Dict[str, Any]], *, fallback_limit: int = 200
    ) -> List[Dict[str, Any]]:
        """Search timesheets with criteria."""
        try:
            return await self.post("/2.0/timesheet/search", criteria)
        except ValueError:
            try:
                return await self.post("/2.0/timesheet/search", {"criteria": criteria})
            except ValueError:
                batch = await self.list_timesheets(limit=fallback_limit)
                return self._filter_by_criteria(batch, criteria)

    # Timesheet Status methods
    async def list_timesheet_statuses(self) -> List[Dict[str, Any]]:
        """Fetch a list of timesheet statuses."""
        return await self.get("/2.0/timesheet_status")

    async def get_timesheet_status(self, status_id: int) -> Dict[str, Any]:
        """Fetch a specific timesheet status."""
        return await self.get(f"/2.0/timesheet_status/{status_id}")

    # Client Service methods
    async def list_client_services(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of client services."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        return await self.get("/2.0/client_service", params=params)

    async def get_client_service(self, client_service_id: int) -> Dict[str, Any]:
        """Fetch a specific client service."""
        return await self.get(f"/2.0/client_service/{client_service_id}")

    async def create_client_service(self, client_service_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new client service.

        Required fields:
        - name: Service name
        - contact_id: Contact ID
        """
        return await self.post("/2.0/client_service", client_service_data)

    async def search_client_services(self, criteria: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Search client services with criteria."""
        return await self.post("/2.0/client_service/search", criteria)

    # Business Activity methods
    async def list_business_activities(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch a list of business activities."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        return await self.get("/2.0/business_activity", params=params)

    async def get_business_activity(self, business_activity_id: int) -> Dict[str, Any]:
        """Fetch a specific business activity."""
        return await self.get(f"/2.0/business_activity/{business_activity_id}")

    async def create_business_activity(self, business_activity_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new business activity.

        Required fields:
        - name: Activity name
        """
        return await self.post("/2.0/business_activity", business_activity_data)

    async def search_business_activities(self, criteria: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Search business activities with criteria."""
        return await self.post("/2.0/business_activity/search", criteria)
