GAME_CATEGORIES = {
    "friends": "Friends",
    "best_friends": "Best Friends",
    "couples": "Couples",
}

PROMPT_CATEGORY_ALIASES = {
    "friend": "friends",
    "friends": "friends",

    "best": "best_friends",
    "bestfriend": "best_friends",
    "best_friend": "best_friends",
    "best_friends": "best_friends",

    "relationship": "couples",
    "couples": "couples",
}


def normalize_prompt_category(value):
    """
    Map legacy prompt categories to the web game's categories.
    Does not modify the original database value.
    """
    if not value:
        return None

    return PROMPT_CATEGORY_ALIASES.get(value.strip().lower())


def get_category_label(value):
    category = normalize_prompt_category(value)

    if category:
        return GAME_CATEGORIES[category]

    return value.replace("_", " ").title() if value else "Uncategorised"
