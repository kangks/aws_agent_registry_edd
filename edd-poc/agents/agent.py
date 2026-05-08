"""
Agent factory and tool definitions for the Multiplier EDD POC.

Provides a configurable HR/compliance agent with 4 tools and support
for multiple Bedrock model backends.
"""

from strands import Agent, tool


# ---------------------------------------------------------------------------
# Supported models
# ---------------------------------------------------------------------------

SUPPORTED_MODELS: dict[str, str] = {
    "sonnet": "us.anthropic.claude-sonnet-4-6",
    "nova_2_pro": "us.amazon.nova-pro-v1:0",
    "glm_5": "zai.glm-5",
}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an HR and compliance assistant for a global workforce management platform.

IMPORTANT: You MUST use the available tools to answer any domain-specific questions.
Do NOT answer HR, payroll, compliance, or leave questions from memory alone.
Always call the appropriate tool first, then summarize the results for the user.

Available tools:
- employee_lookup: Get employment details for an employee
- compliance_checker: Get compliance rules for a country
- payroll_calculator: Calculate payroll breakdown
- leave_manager: Check or manage employee leave

If a question falls outside these domains, you may answer directly."""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def employee_lookup(employee_id: str, country: str) -> dict:
    """Look up employment data for a given employee in a specific country.

    Args:
        employee_id: The unique identifier of the employee.
        country: The country where the employee is based.

    Returns:
        A dict with employment_status, department, salary, and start_date.
    """
    # Mock data keyed by country for demonstration purposes
    mock_data = {
        "singapore": {
            "employment_status": "active",
            "department": "Engineering",
            "salary": 95000.00,
            "start_date": "2022-03-15",
        },
        "thailand": {
            "employment_status": "active",
            "department": "Operations",
            "salary": 72000.00,
            "start_date": "2021-08-01",
        },
        "india": {
            "employment_status": "active",
            "department": "Product",
            "salary": 55000.00,
            "start_date": "2023-01-10",
        },
    }

    country_lower = country.lower()
    if country_lower in mock_data:
        return {
            "employee_id": employee_id,
            "country": country,
            **mock_data[country_lower],
        }

    # Default mock response for any other country
    return {
        "employee_id": employee_id,
        "country": country,
        "employment_status": "active",
        "department": "General",
        "salary": 60000.00,
        "start_date": "2023-06-01",
    }


@tool
def compliance_checker(country: str) -> dict:
    """Get compliance rules and regulations for a specific country.

    Args:
        country: The country to check compliance rules for.

    Returns:
        A dict with notice_periods, benefits, tax_rates, and regulatory_requirements.
        Returns an error dict for unsupported countries.
    """
    supported_countries = {
        "singapore": {
            "notice_periods": {"probation": "1 week", "confirmed": "1 month"},
            "benefits": ["CPF contributions", "Annual leave (14 days)", "Sick leave (14 days)"],
            "tax_rates": {"resident": "0-22%", "non_resident": "15% flat"},
            "regulatory_requirements": [
                "Employment Act compliance",
                "CPF contributions mandatory",
                "Work pass required for foreigners",
            ],
        },
        "thailand": {
            "notice_periods": {"probation": "none", "confirmed": "1 pay cycle"},
            "benefits": ["Social Security Fund", "Annual leave (6 days min)", "Sick leave (30 days)"],
            "tax_rates": {"resident": "0-35%", "non_resident": "15% flat"},
            "regulatory_requirements": [
                "Labour Protection Act compliance",
                "Social Security contributions",
                "Work permit for foreigners",
            ],
        },
        "india": {
            "notice_periods": {"probation": "1 month", "confirmed": "1-3 months"},
            "benefits": ["PF contributions", "Annual leave (15 days)", "Sick leave (12 days)"],
            "tax_rates": {"resident": "0-30%", "non_resident": "30% flat"},
            "regulatory_requirements": [
                "Shops and Establishments Act",
                "PF and ESI mandatory",
                "Gratuity after 5 years",
            ],
        },
    }

    country_lower = country.lower()
    if country_lower in supported_countries:
        return {"country": country, **supported_countries[country_lower]}

    return {
        "error": "Unsupported country",
        "supported": list(supported_countries.keys()),
    }


@tool
def payroll_calculator(annual_salary: float, country: str, currency: str) -> dict:
    """Calculate monthly payroll breakdown for an employee.

    Args:
        annual_salary: The employee's annual gross salary.
        country: The country for tax/social security calculations.
        currency: The currency code (e.g., SGD, THB, INR).

    Returns:
        A dict with gross_monthly, tax, social_security, net_monthly, and employer_cost.
    """
    # Tax and social security rates by country (simplified mock rates)
    country_rates = {
        "singapore": {"tax_rate": 0.15, "employee_ss": 0.20, "employer_ss": 0.17},
        "thailand": {"tax_rate": 0.20, "employee_ss": 0.05, "employer_ss": 0.05},
        "india": {"tax_rate": 0.20, "employee_ss": 0.12, "employer_ss": 0.12},
    }

    # Default rates for unsupported countries
    rates = country_rates.get(country.lower(), {"tax_rate": 0.18, "employee_ss": 0.10, "employer_ss": 0.10})

    gross_monthly = annual_salary / 12
    tax = gross_monthly * rates["tax_rate"]
    social_security = gross_monthly * rates["employee_ss"]
    net_monthly = gross_monthly - tax - social_security
    employer_cost = gross_monthly + (gross_monthly * rates["employer_ss"])

    return {
        "currency": currency,
        "country": country,
        "gross_monthly": round(gross_monthly, 2),
        "tax": round(tax, 2),
        "social_security": round(social_security, 2),
        "net_monthly": round(net_monthly, 2),
        "employer_cost": round(employer_cost, 2),
    }


@tool
def leave_manager(employee_id: str, action: str) -> dict:
    """Check or manage employee leave balances.

    Args:
        employee_id: The unique identifier of the employee.
        action: The action to perform (e.g., 'check_balance', 'request', 'history').

    Returns:
        A dict with annual_entitlement, used_days, and remaining_balance.
    """
    # Mock leave data
    return {
        "employee_id": employee_id,
        "action": action,
        "annual_entitlement": 21,
        "used_days": 8,
        "remaining_balance": 13,
    }


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

def create_agent(model_key: str) -> Agent:
    """Create a configured Strands Agent for the given model.

    Args:
        model_key: One of the keys in SUPPORTED_MODELS (sonnet, haiku, nova_pro).

    Returns:
        A Strands Agent instance configured with the 4 HR/compliance tools.

    Raises:
        ValueError: If model_key is not in SUPPORTED_MODELS.
    """
    if model_key not in SUPPORTED_MODELS:
        raise ValueError(
            f"Unsupported model_key '{model_key}'. "
            f"Supported keys: {list(SUPPORTED_MODELS.keys())}"
        )

    model_id = SUPPORTED_MODELS[model_key]

    return Agent(
        model=model_id,
        system_prompt=SYSTEM_PROMPT,
        tools=[employee_lookup, compliance_checker, payroll_calculator, leave_manager],
    )
