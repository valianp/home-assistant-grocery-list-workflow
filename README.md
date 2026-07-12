# Grocery List Workflow for Home Assistant

An optional Home Assistant custom integration that synchronizes any two native `todo` entities and can format one target list for a configured grocery-store route.

## What it does

- Reconciles additions, removals, and completion status between a source and target to-do list every five minutes.
- Adds `[Route]` checkbox headers and reorders the target list using a private route profile stored in Home Assistant.
- Keeps generated headers local to the target list; they never synchronize back to the source.
- Provides `grocery_list_workflow.sync_now`, `sort_now`, and `sync_and_sort` services.

## Setup

1. Install the source and target list integrations first. For example, install **Skylight Lists** and a Google Keep to-do integration.
2. Install this repository through HACS as a custom integration.
3. Add **Grocery List Workflow** in Settings > Devices & services and select the two entity IDs, for example `todo.skylight_grocery_list` and `todo.google_keep_groceries`.
4. Open the integration's **Configure** dialog and enter the route-profile JSON. The profile contains ordered stops, an item-to-stop mapping, and a fallback stop. It remains in Home Assistant's private config-entry storage rather than this repository.

This integration deliberately has no Skylight or Google credentials. It works through Home Assistant's native to-do API.

## Route profile format

```json
{
  "stops": [
    {"id": "first", "order": 10, "label": "First stop"},
    {"id": "unmapped", "order": 9999, "label": "Unmapped items"}
  ],
  "items": {
    "example item": "first"
  },
  "fallback": "unmapped"
}
```

Keep real store addresses, personal route labels, and shopping mappings in the private profile only.
