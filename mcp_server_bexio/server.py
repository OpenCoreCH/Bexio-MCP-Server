"""MCP server for Bexio integration.

FIELD VALIDATION & AUTO-FILL BEHAVIOR:
=====================================

This MCP server implements intelligent field validation and auto-completion for Bexio API calls.
Understanding the field types and validation behavior is crucial for successful API interactions.

FIELD TYPES:
-----------
1. REQUIRED_USER_INPUT: Fields that MUST be provided by the user
   - Examples: name_1 (contacts), contact_id (invoices), intern_name (items)
   - These fields cannot be auto-filled and will cause validation errors if missing

2. AUTO_FILL_DEFAULT: Fields auto-filled with safe default values if missing
   - user_id: Auto-filled with 1 (default user) - can be overridden by user
   - contact_type_id: Auto-filled with 2 (person) for contacts - can be overridden by user
   - owner_id: Auto-filled with 1 (default owner) - can be overridden by user
   - pr_state_id: Auto-filled with 1 (active) for projects - can be overridden by user
   - pr_project_type_id: Auto-filled with 1 (default type) for projects - can be overridden by user
   - article_type_id: Auto-filled with 1 (default type) for items - can be overridden by user
   - currency_id: Auto-filled with 1 (CHF) for items - can be overridden by user
   - is_stock: Auto-filled with false for items - can be overridden by user
   - delivery_price: Auto-filled with 0 for items - can be overridden by user
   NOTE: User-provided values always take precedence over auto-filled defaults

3. AUTO_FILL_LOOKUP: Fields retrieved from existing data when updating
   - Used in update_contact to preserve existing required fields
   - Ensures updates don't accidentally remove required data

4. API_HANDLED: Fields where the Bexio API provides intelligent defaults
   - nr field: API auto-generates sequential numbers
   - tax_id: API provides fallback tax rates when missing

INVOICE/QUOTE POSITIONS:
-----------------------
Position arrays require special attention:
- type: Auto-filled with 'KbPositionCustom' if missing
- text: REQUIRED - must describe the line item
- amount: Auto-filled with 1 if missing
- unit_price: Auto-filled with 0.0 if missing
- tax_id: Auto-looked up from valid system taxes if missing

ERROR HANDLING:
--------------
422 Validation Errors are enhanced with helpful guidance:
- Missing required fields are detected and explained
- Invalid field values are identified
- Auto-completion attempts are logged
- Original API error messages are preserved for debugging

VALIDATION FLOW:
---------------
1. Pre-validation checks for truly required fields
2. Auto-completion of missing fields with safe defaults
3. API call with completed data
4. Enhanced error messages if validation still fails
5. Retry logic for recoverable validation errors

TESTING RECOMMENDATIONS:
-----------------------
- Test with minimal required fields to verify auto-completion
- Test with invalid field values to verify error handling
- Test update operations to ensure existing data preservation
- Verify tax_id lookup functionality with various scenarios
"""

import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional

# Ensure package imports work whether run as a module or as a script path
if __package__ is None or __package__ == "":
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv, find_dotenv
from mcp.server import Server
from mcp.types import TextContent, Tool
from pydantic import ValidationError

from mcp_server_bexio.bexio_client import BexioClient, BexioConfig
from mcp_server_bexio.field_validator import BexioFieldValidator

# Load environment variables (works even if CWD is not the project root)
_found_env = find_dotenv()
if not _found_env:
    _found_env = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
load_dotenv(_found_env)

# Initialize MCP server
server = Server("bexio-mcp-server")

# Basic stderr breadcrumb to help debug early exits
print("[bexio] server module loaded; env file=", _found_env, file=sys.stderr, flush=True)

# Global Bexio client instance
bexio_client: Optional[BexioClient] = None
field_validator: Optional[BexioFieldValidator] = None


async def get_bexio_client() -> BexioClient:
    """Get or create Bexio client instance."""
    global bexio_client, field_validator
    
    if bexio_client is None:
        try:
            config = BexioConfig(
                access_token=os.getenv("BEXIO_ACCESS_TOKEN", "").strip(),
                api_url=os.getenv("BEXIO_API_URL", "https://api.bexio.com"),
                timeout=int(os.getenv("BEXIO_TIMEOUT", "120")),
            )
            if not config.access_token:
                raise ValueError(
                    "Missing BEXIO_ACCESS_TOKEN. Set it in your Claude config env or in a .env file."
                )
            bexio_client = BexioClient(config)
            field_validator = BexioFieldValidator(bexio_client)
        except ValidationError as e:
            raise ValueError(f"Invalid Bexio configuration: {e}")
        except Exception as e:
            raise ValueError(f"Failed to create Bexio client: {e}")
    
    return bexio_client


@server.list_tools()
async def list_tools() -> List[Tool]:
    """List available tools."""
    return [
        Tool(
            name="search_contacts",
            description="Search for contacts in Bexio using specific criteria. REQUIRED: criteria array with field/value/criteria objects. AUTO-FILLED: limit=50, offset=0.",
            inputSchema={
                "type": "object",
                "properties": {
                    "criteria": {
                        "type": "array",
                        "description": "Search criteria array - each item must have field, value, and criteria",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {
                                    "type": "string", 
                                    "description": "Field to search (e.g., 'name_1', 'name_2', 'email', 'city')"
                                },
                                "value": {
                                    "type": "string", 
                                    "description": "Search value"
                                },
                                "criteria": {
                                    "type": "string", 
                                    "description": "Search criteria: 'like', '=', '!=', '>', '<', '>=', '<=', 'is_null', 'not_null'",
                                    "default": "like"
                                }
                            },
                            "required": ["field", "value", "criteria"]
                        }
                    },
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0}
                },
                "required": ["criteria"]
            }
        ),
        Tool(
            name="get_contact",
            description="Get detailed information about a specific contact. REQUIRED: contact_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "contact_id": {"type": "integer", "description": "Contact ID"}
                },
                "required": ["contact_id"]
            }
        ),
        Tool(
            name="create_contact",
            description="Create a new contact in Bexio. REQUIRED: name_1. AUTO-FILLED: contact_type_id=2, user_id=1, owner_id=1. System handles field validation and auto-completion.",
            inputSchema={
                "type": "object",
                "properties": {
                    "contact_data": {
                        "type": "object",
                        "description": "Contact data",
                        "properties": {
                            "name_1": {"type": "string", "description": "REQUIRED: First name or company name"},
                            "name_2": {"type": "string", "description": "Last name (for persons)"},
                            "contact_type_id": {"type": "integer", "description": "REQUIRED: Contact type (1=company, 2=person). Auto-filled with 2 if missing."},
                            "user_id": {"type": "integer", "description": "REQUIRED: User ID. Auto-filled with 1 if missing."},
                            "owner_id": {"type": "integer", "description": "REQUIRED: Owner ID. Auto-filled with 1 if missing."},
                            "email": {"type": "string", "description": "Email address"},
                            "phone_fixed": {"type": "string", "description": "Fixed phone number"},
                            "phone_mobile": {"type": "string", "description": "Mobile phone number"},
                            "address": {"type": "string", "description": "Street address"},
                            "postcode": {"type": "string", "description": "Postal code"},
                            "city": {"type": "string", "description": "City"},
                            "country_id": {"type": "integer", "description": "Country ID (1=Switzerland, 2=Germany, etc.)"},
                            "language_id": {"type": "integer", "description": "Language ID (1=German, 2=French, 3=Italian, 4=English)"}
                        },
                        "required": ["name_1"]
                    }
                },
                "required": ["contact_data"]
            }
        ),
        Tool(
            name="update_contact",
            description="Update an existing contact. REQUIRED: contact_id. AUTO-RETRIEVED: name_1, contact_type_id, user_id, owner_id, nr from existing contact. System preserves required fields.",
            inputSchema={
                "type": "object",
                "properties": {
                    "contact_id": {"type": "integer", "description": "Contact ID"},
                    "contact_data": {
                        "type": "object",
                        "description": "Updated contact data",
                        "properties": {
                            "name_1": {"type": "string", "description": "REQUIRED: First name or company name. Auto-retrieved from existing contact if missing."},
                            "name_2": {"type": "string", "description": "Last name"},
                            "contact_type_id": {"type": "integer", "description": "REQUIRED: Contact type. Auto-retrieved from existing contact if missing."},
                            "user_id": {"type": "integer", "description": "REQUIRED: User ID. Auto-retrieved from existing contact if missing."},
                            "owner_id": {"type": "integer", "description": "REQUIRED: Owner ID. Auto-retrieved from existing contact if missing."},
                            "nr": {"type": "string", "description": "Contact number (auto-generated). API handles gracefully if missing."},
                            "mail": {"type": "string", "description": "Email address"},
                            "phone_fixed": {"type": "string", "description": "Phone number"},
                            "address": {"type": "string", "description": "Street address"},
                            "postcode": {"type": "string", "description": "Postal code"},
                            "city": {"type": "string", "description": "City"}
                        }
                    }
                },
                "required": ["contact_id", "contact_data"]
            }
        ),
        Tool(
            name="list_contacts",
            description="List all contacts with optional filtering. AUTO-FILLED: limit=50, offset=0, show_archived=false. Optional: order_by.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0},
                    "order_by": {"type": "string", "description": "Field to order by"},
                    "show_archived": {"type": "boolean", "description": "Include archived contacts in results", "default": False}
                }
            }
        ),
        Tool(
            name="search_invoices",
            description="Search for invoices in Bexio. REQUIRED: criteria array with field/value/criteria objects. AUTO-FILLED: limit=50, offset=0.",
            inputSchema={
                "type": "object",
                "properties": {
                    "criteria": {
                        "type": "array",
                        "description": "Search criteria array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string", "description": "Field name to search"},
                                "value": {"type": "string", "description": "Value to search for"},
                                "criteria": {"type": "string", "description": "Search criteria (=, like, etc.)"}
                            },
                            "required": ["field", "value", "criteria"]
                        }
                    },
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0}
                }
            }
        ),
        Tool(
            name="get_invoice",
            description="Get detailed information about a specific invoice. REQUIRED: invoice_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "invoice_id": {"type": "integer", "description": "Invoice ID"}
                },
                "required": ["invoice_id"]
            }
        ),
        Tool(
            name="create_invoice",
            description="Create a new invoice in Bexio. REQUIRED: contact_id, positions array with text field. AUTO-FILLED: user_id=1, position type/amount/unit_price, tax_id lookup. System handles validation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "contact_id": {"type": "integer", "description": "REQUIRED: Contact ID for the invoice"},
                    "user_id": {"type": "integer", "description": "REQUIRED: User ID. Auto-filled with 1 if missing."},
                    "nr": {"type": "string", "description": "Invoice number. API auto-generates if missing."},
                    "title": {"type": "string", "description": "Invoice title"},
                    "positions": {
                        "type": "array",
                        "description": "REQUIRED: Invoice line items array. Each position needs: type, text, amount, unit_price, tax_id",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "description": "REQUIRED: Position type. Auto-filled with 'KbPositionCustom' if missing."},
                                "text": {"type": "string", "description": "REQUIRED: Item description. Auto-filled with 'Service' if missing."},
                                "amount": {"type": "number", "description": "REQUIRED: Quantity. Auto-filled with 1 if missing."},
                                "unit_price": {"type": "number", "description": "REQUIRED: Unit price. Auto-filled with 0.0 if missing."},
                                "tax_id": {"type": "integer", "description": "REQUIRED: Tax ID. Auto-looked up from valid system taxes if missing."}
                            },
                            "required": ["text"]
                        }
                    }
                },
                "required": ["contact_id", "positions"]
            }
        ),
        Tool(
            name="list_invoices",
            description="List all invoices with optional filtering. AUTO-FILLED: limit=50, offset=0. Optional: order_by.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0},
                    "order_by": {"type": "string", "description": "Field to order by"}
                }
            }
        ),
        Tool(
            name="search_quotes",
            description="Search for quotes in Bexio. REQUIRED: criteria array with field/value/criteria objects. AUTO-FILLED: limit=50, offset=0.",
            inputSchema={
                "type": "object",
                "properties": {
                    "criteria": {
                        "type": "array",
                        "description": "Search criteria array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string", "description": "Field name to search"},
                                "value": {"type": "string", "description": "Value to search for"},
                                "criteria": {"type": "string", "description": "Search criteria (=, like, etc.)"}
                            },
                            "required": ["field", "value", "criteria"]
                        }
                    },
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0}
                }
            }
        ),
        Tool(
            name="get_quote",
            description="Get detailed information about a specific quote. REQUIRED: quote_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "quote_id": {"type": "integer", "description": "Quote ID"}
                },
                "required": ["quote_id"]
            }
        ),
        Tool(
            name="create_quote",
            description="Create a new quote in Bexio. REQUIRED: contact_id. AUTO-FILLED: user_id=1. Positions optional but recommended with same auto-fill as invoices. System handles validation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "contact_id": {"type": "integer", "description": "REQUIRED: Contact ID for the quote"},
                    "user_id": {"type": "integer", "description": "REQUIRED: User ID. Auto-filled with 1 if missing."},
                    "nr": {"type": "string", "description": "Quote number. API auto-generates if missing."},
                    "title": {"type": "string", "description": "Quote title"},
                    "positions": {
                        "type": "array",
                        "description": "Quote line items (optional for quotes, but recommended)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "description": "REQUIRED: Position type. Auto-filled with 'KbPositionCustom' if missing."},
                                "text": {"type": "string", "description": "REQUIRED: Item description. Auto-filled with 'Service' if missing."},
                                "amount": {"type": "number", "description": "REQUIRED: Quantity. Auto-filled with 1 if missing."},
                                "unit_price": {"type": "number", "description": "REQUIRED: Unit price. Auto-filled with 0.0 if missing."},
                                "tax_id": {"type": "integer", "description": "REQUIRED: Tax ID. Auto-looked up from valid system taxes if missing."}
                            },
                            "required": ["text"]
                        }
                    }
                },
                "required": ["contact_id"]
            }
        ),
        Tool(
            name="list_projects",
            description="List all projects with optional filtering. AUTO-FILLED: limit=50, offset=0. Optional: order_by.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0},
                    "order_by": {"type": "string", "description": "Field to order by"}
                }
            }
        ),
        Tool(
            name="get_project",
            description="Get detailed information about a specific project. REQUIRED: project_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Project ID"}
                },
                "required": ["project_id"]
            }
        ),
        Tool(
            name="create_project",
            description="Create a new project in Bexio. REQUIRED: name, contact_id. AUTO-FILLED: user_id=1, pr_state_id=1, pr_project_type_id=1. System handles field validation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_data": {
                        "type": "object",
                        "description": "Project data",
                        "properties": {
                            "name": {"type": "string", "description": "REQUIRED: Project name"},
                            "contact_id": {"type": "integer", "description": "REQUIRED: Contact ID for the project"},
                            "user_id": {"type": "integer", "description": "REQUIRED: User ID. Auto-filled with 1 if missing."},
                            "nr": {"type": "string", "description": "Project number. API auto-generates if missing."},
                            "pr_project_type_id": {"type": "integer", "description": "REQUIRED: Project type ID. Auto-filled with 1 if missing."},
                            "pr_state_id": {"type": "integer", "description": "REQUIRED: Project state ID. Auto-filled with 1 if missing."}
                        },
                        "required": ["name", "contact_id"]
                    }
                },
                "required": ["project_data"]
            }
        ),
        Tool(
            name="list_items",
            description="List all items/articles with optional filtering. AUTO-FILLED: limit=50, offset=0. Optional: order_by.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0},
                    "order_by": {"type": "string", "description": "Field to order by"}
                }
            }
        ),
        Tool(
            name="get_item",
            description="Get detailed information about a specific item/article. REQUIRED: item_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer", "description": "Item ID"}
                },
                "required": ["item_id"]
            }
        ),
        Tool(
            name="create_item",
            description="Create a new item/article in Bexio. REQUIRED: intern_name. AUTO-FILLED: user_id=1, article_type_id=1, currency_id=1, is_stock=false, delivery_price=0. System handles validation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "item_data": {
                        "type": "object",
                        "description": "Item data",
                        "properties": {
                            "intern_name": {"type": "string", "description": "REQUIRED: Internal item name"},
                            "user_id": {"type": "integer", "description": "REQUIRED: User ID. Auto-filled with 1 if missing."},
                            "nr": {"type": "string", "description": "Item number. API auto-generates if missing."},
                            "article_type_id": {"type": "integer", "description": "REQUIRED: Article type ID. Auto-filled with 1 if missing."},
                            "contact_id": {"type": "integer", "description": "Supplier contact ID"},
                            "deliverer_code": {"type": "string", "description": "Supplier article number"},
                            "deliverer_name": {"type": "string", "description": "Supplier name"},
                            "deliverer_description": {"type": "string", "description": "Supplier description"},
                            "intern_code": {"type": "string", "description": "Internal article number"},
                            "intern_description": {"type": "string", "description": "Internal description"},
                            "purchase_price": {"type": "number", "description": "Purchase price"},
                            "sale_price": {"type": "number", "description": "Sale price"},
                            "purchase_total": {"type": "number", "description": "Total purchase amount"},
                            "sale_total": {"type": "number", "description": "Total sale amount"},
                            "currency_id": {"type": "integer", "description": "REQUIRED: Currency ID. Auto-filled with 1 if missing."},
                            "tax_income_id": {"type": "integer", "description": "Income tax ID"},
                            "tax_id": {"type": "integer", "description": "Tax ID"},
                            "tax_expense_id": {"type": "integer", "description": "Expense tax ID"},
                            "unit_id": {"type": "integer", "description": "Unit ID"},
                            "is_stock": {"type": "boolean", "description": "REQUIRED: Is stock item. Auto-filled with false if missing."},
                            "stock_id": {"type": "integer", "description": "Stock ID"},
                            "stock_min": {"type": "number", "description": "Minimum stock"},
                            "stock_reserved": {"type": "number", "description": "Reserved stock"},
                            "stock_available": {"type": "number", "description": "Available stock"},
                            "stock_picked": {"type": "number", "description": "Picked stock"},
                            "stock_disposed": {"type": "number", "description": "Disposed stock"},
                            "stock_ordered": {"type": "number", "description": "Ordered stock"},
                            "width": {"type": "number", "description": "Width"},
                            "height": {"type": "number", "description": "Height"},
                            "weight": {"type": "number", "description": "Weight"},
                            "volume": {"type": "number", "description": "Volume"},
                            "html_text": {"type": "string", "description": "HTML description"},
                            "remarks": {"type": "string", "description": "Remarks"},
                            "delivery_price": {"type": "number", "description": "REQUIRED: Delivery price. Auto-filled with 0 if missing."},
                            "article_group_id": {"type": "integer", "description": "Article group ID"}
                        },
                        "required": ["intern_name"]
                    }
                },
                "required": ["item_data"]
            }
        ),
        # ==================== ACCOUNTING TOOLS ====================
        # Accounts (Chart of Accounts)
        Tool(
            name="list_accounts",
            description="List all accounts from the chart of accounts. AUTO-FILLED: limit=50, offset=0. Optional: order_by.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0},
                    "order_by": {"type": "string", "description": "Field to order by"}
                }
            }
        ),
        Tool(
            name="get_account",
            description="Get detailed information about a specific account. REQUIRED: account_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer", "description": "Account ID"}
                },
                "required": ["account_id"]
            }
        ),
        Tool(
            name="search_accounts",
            description="Search for accounts in Bexio using specific criteria. REQUIRED: criteria array with field/value/criteria objects.",
            inputSchema={
                "type": "object",
                "properties": {
                    "criteria": {
                        "type": "array",
                        "description": "Search criteria array - each item must have field, value, and criteria",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string", "description": "Field to search (e.g., 'account_no', 'name')"},
                                "value": {"type": "string", "description": "Search value"},
                                "criteria": {"type": "string", "description": "Search criteria: 'like', '=', '!=', etc.", "default": "like"}
                            },
                            "required": ["field", "value", "criteria"]
                        }
                    }
                },
                "required": ["criteria"]
            }
        ),
        # Account Groups
        Tool(
            name="list_account_groups",
            description="List all account groups. AUTO-FILLED: limit=50, offset=0.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0}
                }
            }
        ),
        Tool(
            name="get_account_group",
            description="Get detailed information about a specific account group. REQUIRED: account_group_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account_group_id": {"type": "integer", "description": "Account Group ID"}
                },
                "required": ["account_group_id"]
            }
        ),
        # Taxes
        Tool(
            name="list_taxes",
            description="List all taxes configured in Bexio. AUTO-FILLED: limit=50, offset=0, scope='active'. Set scope=null to include inactive taxes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0},
                    "scope": {"type": ["string", "null"], "description": "Filter scope: 'active' (default) or null for all taxes", "default": "active"}
                }
            }
        ),
        Tool(
            name="get_tax",
            description="Get detailed information about a specific tax. REQUIRED: tax_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tax_id": {"type": "integer", "description": "Tax ID"}
                },
                "required": ["tax_id"]
            }
        ),
        # Currencies
        Tool(
            name="list_currencies",
            description="List all currencies configured in Bexio. AUTO-FILLED: limit=50, offset=0.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0}
                }
            }
        ),
        Tool(
            name="get_currency",
            description="Get detailed information about a specific currency. REQUIRED: currency_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "currency_id": {"type": "integer", "description": "Currency ID"}
                },
                "required": ["currency_id"]
            }
        ),
        Tool(
            name="create_currency",
            description="Create a new currency in Bexio. REQUIRED: name, round_factor.",
            inputSchema={
                "type": "object",
                "properties": {
                    "currency_data": {
                        "type": "object",
                        "description": "Currency data",
                        "properties": {
                            "name": {"type": "string", "description": "REQUIRED: Currency code (e.g., 'EUR', 'USD')"},
                            "round_factor": {"type": "number", "description": "REQUIRED: Rounding factor (e.g., 0.05 for CHF)"}
                        },
                        "required": ["name", "round_factor"]
                    }
                },
                "required": ["currency_data"]
            }
        ),
        Tool(
            name="get_exchange_rates",
            description="Get exchange rates for currencies. Optional: date parameter for historical rates.",
            inputSchema={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date for exchange rates (YYYY-MM-DD format). Defaults to current date."}
                }
            }
        ),
        # Manual Entries / Accounting Journal
        Tool(
            name="list_manual_entries",
            description="List all manual entries (accounting journal bookings). AUTO-FILLED: limit=50, offset=0. Optional: order_by.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0},
                    "order_by": {"type": "string", "description": "Field to order by"}
                }
            }
        ),
        Tool(
            name="get_manual_entry",
            description="Get detailed information about a specific manual entry. REQUIRED: entry_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "entry_id": {"type": "integer", "description": "Manual Entry ID"}
                },
                "required": ["entry_id"]
            }
        ),
        Tool(
            name="create_manual_entry",
            description="Create a new manual entry (accounting journal booking). REQUIRED: date, debit_account_id, credit_account_id, amount. Optional: tax_id, text, reference_nr.",
            inputSchema={
                "type": "object",
                "properties": {
                    "entry_data": {
                        "type": "object",
                        "description": "Manual entry data",
                        "properties": {
                            "date": {"type": "string", "description": "REQUIRED: Booking date (YYYY-MM-DD format)"},
                            "debit_account_id": {"type": "integer", "description": "REQUIRED: Debit account ID (from chart of accounts)"},
                            "credit_account_id": {"type": "integer", "description": "REQUIRED: Credit account ID (from chart of accounts)"},
                            "amount": {"type": "number", "description": "REQUIRED: Booking amount"},
                            "tax_id": {"type": "integer", "description": "Tax ID for VAT (optional)"},
                            "text": {"type": "string", "description": "Description/reference text (optional)"},
                            "reference_nr": {"type": "string", "description": "Reference number (optional, auto-generated if missing)"},
                            "currency_id": {"type": "integer", "description": "Currency ID (optional, defaults to base currency)"},
                            "currency_factor": {"type": "number", "description": "Exchange rate factor (optional)"}
                        },
                        "required": ["date", "debit_account_id", "credit_account_id", "amount"]
                    }
                },
                "required": ["entry_data"]
            }
        ),
        Tool(
            name="get_next_reference_number",
            description="Get the next available reference number for manual entries.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        # Journal Report (read-only accounting ledger)
        Tool(
            name="get_journal",
            description="Get accounting journal/ledger entries (read-only report). Optional filters: from_date, to_date, account_uuid. AUTO-FILLED: limit=500, offset=0.",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_date": {"type": "string", "description": "Start date filter (YYYY-MM-DD format)"},
                    "to_date": {"type": "string", "description": "End date filter (YYYY-MM-DD format)"},
                    "account_uuid": {"type": "string", "description": "Filter by specific account UUID"},
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 500},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0}
                }
            }
        ),
        # Business Years
        Tool(
            name="list_business_years",
            description="List all business years. AUTO-FILLED: limit=50, offset=0.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0}
                }
            }
        ),
        Tool(
            name="get_business_year",
            description="Get detailed information about a specific business year. REQUIRED: business_year_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "business_year_id": {"type": "integer", "description": "Business Year ID"}
                },
                "required": ["business_year_id"]
            }
        ),
        # Calendar Years
        Tool(
            name="list_calendar_years",
            description="List all calendar years. AUTO-FILLED: limit=50, offset=0.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0}
                }
            }
        ),
        Tool(
            name="get_calendar_year",
            description="Get detailed information about a specific calendar year. REQUIRED: calendar_year_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "calendar_year_id": {"type": "integer", "description": "Calendar Year ID"}
                },
                "required": ["calendar_year_id"]
            }
        ),
        Tool(
            name="create_calendar_year",
            description="Create a new calendar year. REQUIRED: start, end dates.",
            inputSchema={
                "type": "object",
                "properties": {
                    "calendar_year_data": {
                        "type": "object",
                        "description": "Calendar year data",
                        "properties": {
                            "start": {"type": "string", "description": "REQUIRED: Start date (YYYY-MM-DD format)"},
                            "end": {"type": "string", "description": "REQUIRED: End date (YYYY-MM-DD format)"}
                        },
                        "required": ["start", "end"]
                    }
                },
                "required": ["calendar_year_data"]
            }
        ),
        # VAT Periods
        Tool(
            name="list_vat_periods",
            description="List all VAT periods. AUTO-FILLED: limit=50, offset=0.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0}
                }
            }
        ),
        Tool(
            name="get_vat_period",
            description="Get detailed information about a specific VAT period. REQUIRED: vat_period_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "vat_period_id": {"type": "integer", "description": "VAT Period ID"}
                },
                "required": ["vat_period_id"]
            }
        ),
        # ==================== TIMESHEET TOOLS ====================
        # Timesheets
        Tool(
            name="list_timesheets",
            description="List all timesheet entries. AUTO-FILLED: limit=50, offset=0. Optional: order_by.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0},
                    "order_by": {"type": "string", "description": "Field to order by"}
                }
            }
        ),
        Tool(
            name="get_timesheet",
            description="Get detailed information about a specific timesheet entry. REQUIRED: timesheet_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timesheet_id": {"type": "integer", "description": "Timesheet ID"}
                },
                "required": ["timesheet_id"]
            }
        ),
        Tool(
            name="create_timesheet",
            description="Create a new timesheet entry. REQUIRED: user_id, client_service_id, date, duration. Optional: allowable_bill, text, contact_id, pr_project_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timesheet_data": {
                        "type": "object",
                        "description": "Timesheet data",
                        "properties": {
                            "user_id": {"type": "integer", "description": "REQUIRED: User ID performing the work"},
                            "client_service_id": {"type": "integer", "description": "REQUIRED: Client service ID"},
                            "date": {"type": "string", "description": "REQUIRED: Date of the timesheet entry (YYYY-MM-DD format)"},
                            "duration": {"type": "string", "description": "REQUIRED: Duration in HH:MM format (e.g., '02:30' for 2.5 hours)"},
                            "allowable_bill": {"type": "boolean", "description": "Whether the time is billable (default: false)"},
                            "text": {"type": "string", "description": "Description text"},
                            "contact_id": {"type": "integer", "description": "Contact ID"},
                            "pr_project_id": {"type": "integer", "description": "Project ID"},
                            "tracking_type": {"type": "integer", "description": "Type of tracking (0 or 1)"},
                            "status_id": {"type": "integer", "description": "Timesheet status ID"}
                        },
                        "required": ["user_id", "client_service_id", "date", "duration"]
                    }
                },
                "required": ["timesheet_data"]
            }
        ),
        Tool(
            name="search_timesheets",
            description="Search for timesheet entries using specific criteria. REQUIRED: criteria array with field/value/criteria objects.",
            inputSchema={
                "type": "object",
                "properties": {
                    "criteria": {
                        "type": "array",
                        "description": "Search criteria array - each item must have field, value, and criteria",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string", "description": "Field to search (e.g., 'user_id', 'date', 'client_service_id')"},
                                "value": {"type": "string", "description": "Search value"},
                                "criteria": {"type": "string", "description": "Search criteria: 'like', '=', '!=', etc.", "default": "="}
                            },
                            "required": ["field", "value", "criteria"]
                        }
                    }
                },
                "required": ["criteria"]
            }
        ),
        Tool(
            name="update_timesheet",
            description="Update an existing timesheet entry. REQUIRED: timesheet_id, timesheet_data.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timesheet_id": {"type": "integer", "description": "Timesheet ID to update"},
                    "timesheet_data": {
                        "type": "object",
                        "description": "Updated timesheet data",
                        "properties": {
                            "user_id": {"type": "integer", "description": "User ID performing the work"},
                            "client_service_id": {"type": "integer", "description": "Client service ID"},
                            "date": {"type": "string", "description": "Date of the timesheet entry (YYYY-MM-DD format)"},
                            "duration": {"type": "string", "description": "Duration in HH:MM format (e.g., '02:30')"},
                            "allowable_bill": {"type": "boolean", "description": "Whether the time is billable"},
                            "text": {"type": "string", "description": "Description text"},
                            "contact_id": {"type": "integer", "description": "Contact ID"},
                            "pr_project_id": {"type": "integer", "description": "Project ID"},
                            "status_id": {"type": "integer", "description": "Timesheet status ID"}
                        }
                    }
                },
                "required": ["timesheet_id", "timesheet_data"]
            }
        ),
        Tool(
            name="delete_timesheet",
            description="Delete a timesheet entry. REQUIRED: timesheet_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timesheet_id": {"type": "integer", "description": "Timesheet ID to delete"}
                },
                "required": ["timesheet_id"]
            }
        ),
        # Timesheet Status
        Tool(
            name="list_timesheet_statuses",
            description="List all available timesheet statuses.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="get_timesheet_status",
            description="Get detailed information about a specific timesheet status. REQUIRED: status_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status_id": {"type": "integer", "description": "Timesheet Status ID"}
                },
                "required": ["status_id"]
            }
        ),
        # Client Services
        Tool(
            name="list_client_services",
            description="List all client services. AUTO-FILLED: limit=50, offset=0.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0}
                }
            }
        ),
        Tool(
            name="get_client_service",
            description="Get detailed information about a specific client service. REQUIRED: client_service_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "client_service_id": {"type": "integer", "description": "Client Service ID"}
                },
                "required": ["client_service_id"]
            }
        ),
        Tool(
            name="create_client_service",
            description="Create a new client service. REQUIRED: name, contact_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "client_service_data": {
                        "type": "object",
                        "description": "Client service data",
                        "properties": {
                            "name": {"type": "string", "description": "REQUIRED: Service name"},
                            "contact_id": {"type": "integer", "description": "REQUIRED: Contact ID"}
                        },
                        "required": ["name", "contact_id"]
                    }
                },
                "required": ["client_service_data"]
            }
        ),
        Tool(
            name="search_client_services",
            description="Search for client services using specific criteria. REQUIRED: criteria array.",
            inputSchema={
                "type": "object",
                "properties": {
                    "criteria": {
                        "type": "array",
                        "description": "Search criteria array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string", "description": "Field to search (e.g., 'name', 'contact_id')"},
                                "value": {"type": "string", "description": "Search value"},
                                "criteria": {"type": "string", "description": "Search criteria: 'like', '=', etc.", "default": "like"}
                            },
                            "required": ["field", "value", "criteria"]
                        }
                    }
                },
                "required": ["criteria"]
            }
        ),
        # Business Activities
        Tool(
            name="list_business_activities",
            description="List all business activities. AUTO-FILLED: limit=50, offset=0.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum number of results", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip", "default": 0}
                }
            }
        ),
        Tool(
            name="get_business_activity",
            description="Get detailed information about a specific business activity. REQUIRED: business_activity_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "business_activity_id": {"type": "integer", "description": "Business Activity ID"}
                },
                "required": ["business_activity_id"]
            }
        ),
        Tool(
            name="create_business_activity",
            description="Create a new business activity. REQUIRED: name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "business_activity_data": {
                        "type": "object",
                        "description": "Business activity data",
                        "properties": {
                            "name": {"type": "string", "description": "REQUIRED: Activity name"}
                        },
                        "required": ["name"]
                    }
                },
                "required": ["business_activity_data"]
            }
        ),
        Tool(
            name="search_business_activities",
            description="Search for business activities using specific criteria. REQUIRED: criteria array.",
            inputSchema={
                "type": "object",
                "properties": {
                    "criteria": {
                        "type": "array",
                        "description": "Search criteria array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string", "description": "Field to search (e.g., 'name')"},
                                "value": {"type": "string", "description": "Search value"},
                                "criteria": {"type": "string", "description": "Search criteria: 'like', '=', etc.", "default": "like"}
                            },
                            "required": ["field", "value", "criteria"]
                        }
                    }
                },
                "required": ["criteria"]
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    """Handle tool calls."""
    try:
        client = await get_bexio_client()
        global field_validator
        
        # Auto-complete fields for creation/update operations
        if name in ["create_contact", "update_contact", "create_invoice", "update_invoice", "create_quote", "update_quote", "create_project", "update_project", "create_item"]:
            arguments = await field_validator.auto_complete_fields(name, arguments)
        
        if name == "search_contacts":
            criteria = arguments.get("criteria", [])
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            
            # Format search payload according to Bexio API requirements
            search_payload = criteria
            result = await client.search_contacts(search_payload)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "get_contact":
            contact_id = arguments["contact_id"]
            result = await client.get_contact(contact_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "create_contact":
            contact_data = arguments["contact_data"]
            result = await client.create_contact(contact_data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "update_contact":
            contact_id = arguments["contact_id"]
            contact_data = arguments["contact_data"]
            result = await client.update_contact(contact_id, contact_data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "list_contacts":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            order_by = arguments.get("order_by")
            show_archived = arguments.get("show_archived", False)

            result = await client.list_contacts(limit=limit, offset=offset, order_by=order_by, show_archived=show_archived)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "search_invoices":
            criteria = arguments.get("criteria", [])
            result = await client.search_invoices(criteria)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "get_invoice":
            invoice_id = arguments["invoice_id"]
            result = await client.get_invoice(invoice_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "create_invoice":
            result = await client.create_invoice(arguments)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "list_invoices":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            order_by = arguments.get("order_by")
            
            result = await client.list_invoices(limit=limit, offset=offset, order_by=order_by)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "search_quotes":
            criteria = arguments.get("criteria", [])
            result = await client.search_quotes(criteria)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "get_quote":
            quote_id = arguments["quote_id"]
            result = await client.get_quote(quote_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "create_quote":
            result = await client.create_quote(arguments)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "list_projects":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            order_by = arguments.get("order_by")
            
            result = await client.list_projects(limit=limit, offset=offset, order_by=order_by)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "get_project":
            project_id = arguments["project_id"]
            result = await client.get_project(project_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "create_project":
            project_data = arguments["project_data"]
            result = await client.create_project(project_data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "list_items":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            order_by = arguments.get("order_by")
            
            result = await client.list_items(limit=limit, offset=offset, order_by=order_by)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "get_item":
            item_id = arguments["item_id"]
            result = await client.get_item(item_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "create_item":
            item_data = arguments["item_data"]
            result = await client.create_item(item_data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # ==================== ACCOUNTING TOOL HANDLERS ====================

        # Accounts (Chart of Accounts)
        elif name == "list_accounts":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            order_by = arguments.get("order_by")
            result = await client.list_accounts(limit=limit, offset=offset, order_by=order_by)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_account":
            account_id = arguments["account_id"]
            result = await client.get_account(account_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "search_accounts":
            criteria = arguments.get("criteria", [])
            result = await client.search_accounts(criteria)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # Account Groups
        elif name == "list_account_groups":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            result = await client.list_account_groups(limit=limit, offset=offset)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_account_group":
            account_group_id = arguments["account_group_id"]
            result = await client.get_account_group(account_group_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # Taxes
        elif name == "list_taxes":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            scope = arguments.get("scope", "active")
            result = await client.list_taxes(limit=limit, offset=offset, scope=scope)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_tax":
            tax_id = arguments["tax_id"]
            result = await client.get_tax(tax_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # Currencies
        elif name == "list_currencies":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            result = await client.list_currencies(limit=limit, offset=offset)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_currency":
            currency_id = arguments["currency_id"]
            result = await client.get_currency(currency_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "create_currency":
            currency_data = arguments["currency_data"]
            result = await client.create_currency(currency_data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_exchange_rates":
            date = arguments.get("date")
            result = await client.get_exchange_rates(date=date)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # Manual Entries / Accounting Journal
        elif name == "list_manual_entries":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            order_by = arguments.get("order_by")
            result = await client.list_manual_entries(limit=limit, offset=offset, order_by=order_by)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_manual_entry":
            entry_id = arguments["entry_id"]
            result = await client.get_manual_entry(entry_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "create_manual_entry":
            entry_data = arguments["entry_data"]
            result = await client.create_manual_entry(entry_data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_next_reference_number":
            result = await client.get_next_reference_number()
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # Journal Report
        elif name == "get_journal":
            from_date = arguments.get("from_date")
            to_date = arguments.get("to_date")
            account_uuid = arguments.get("account_uuid")
            limit = arguments.get("limit", 500)
            offset = arguments.get("offset", 0)
            result = await client.get_journal(from_date=from_date, to_date=to_date, account_uuid=account_uuid, limit=limit, offset=offset)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # Business Years
        elif name == "list_business_years":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            result = await client.list_business_years(limit=limit, offset=offset)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_business_year":
            business_year_id = arguments["business_year_id"]
            result = await client.get_business_year(business_year_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # Calendar Years
        elif name == "list_calendar_years":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            result = await client.list_calendar_years(limit=limit, offset=offset)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_calendar_year":
            calendar_year_id = arguments["calendar_year_id"]
            result = await client.get_calendar_year(calendar_year_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "create_calendar_year":
            calendar_year_data = arguments["calendar_year_data"]
            result = await client.create_calendar_year(calendar_year_data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # VAT Periods
        elif name == "list_vat_periods":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            result = await client.list_vat_periods(limit=limit, offset=offset)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_vat_period":
            vat_period_id = arguments["vat_period_id"]
            result = await client.get_vat_period(vat_period_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # ==================== TIMESHEET TOOL HANDLERS ====================

        # Timesheets
        elif name == "list_timesheets":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            order_by = arguments.get("order_by")
            result = await client.list_timesheets(limit=limit, offset=offset, order_by=order_by)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_timesheet":
            timesheet_id = arguments["timesheet_id"]
            result = await client.get_timesheet(timesheet_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "create_timesheet":
            timesheet_data = arguments["timesheet_data"]
            result = await client.create_timesheet(timesheet_data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "search_timesheets":
            criteria = arguments.get("criteria", [])
            result = await client.search_timesheets(criteria)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "update_timesheet":
            timesheet_id = arguments["timesheet_id"]
            timesheet_data = arguments["timesheet_data"]
            result = await client.update_timesheet(timesheet_id, timesheet_data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "delete_timesheet":
            timesheet_id = arguments["timesheet_id"]
            await client.delete_timesheet(timesheet_id)
            return [TextContent(type="text", text=json.dumps({"success": True, "message": f"Timesheet {timesheet_id} deleted successfully"}, indent=2))]

        # Timesheet Status
        elif name == "list_timesheet_statuses":
            result = await client.list_timesheet_statuses()
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_timesheet_status":
            status_id = arguments["status_id"]
            result = await client.get_timesheet_status(status_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # Client Services
        elif name == "list_client_services":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            result = await client.list_client_services(limit=limit, offset=offset)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_client_service":
            client_service_id = arguments["client_service_id"]
            result = await client.get_client_service(client_service_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "create_client_service":
            client_service_data = arguments["client_service_data"]
            result = await client.create_client_service(client_service_data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "search_client_services":
            criteria = arguments.get("criteria", [])
            result = await client.search_client_services(criteria)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # Business Activities
        elif name == "list_business_activities":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            result = await client.list_business_activities(limit=limit, offset=offset)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_business_activity":
            business_activity_id = arguments["business_activity_id"]
            result = await client.get_business_activity(business_activity_id)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "create_business_activity":
            business_activity_data = arguments["business_activity_data"]
            result = await client.create_business_activity(business_activity_data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "search_business_activities":
            criteria = arguments.get("criteria", [])
            result = await client.search_business_activities(criteria)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    
    except Exception as e:
        error_msg = str(e)
        
        # Enhanced 422 error handling
        if "422" in error_msg or "HTTP 422" in error_msg:
            if field_validator:
                helpful_msg = field_validator.create_helpful_error_message(error_msg)
                return [TextContent(type="text", text=helpful_msg)]
        
        return [TextContent(type="text", text=f"Error: {error_msg}")]


async def main():
    """Main entry point for the server."""
    from mcp.server.stdio import stdio_server
    
    print("[bexio] entering stdio_server context", file=sys.stderr, flush=True)
    try:
        async with stdio_server() as (read_stream, write_stream):
            print("[bexio] starting server.run", file=sys.stderr, flush=True)
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options()
            )
            print("[bexio] server.run completed", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[bexio] server.run failed: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        import traceback
        print("Bexio MCP server crashed with an unhandled exception:", file=sys.stderr)
        traceback.print_exc()
        sys.stderr.flush()
        sys.exit(1)
