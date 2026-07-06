"""Policy data for the Gmail sorter.

These are the user-maintained cleanup rules: protected categories, keyword
groups, category rules, and scoring defaults. They live in their own module so
they can be edited (and later config-driven via ``config/policy.yaml``) without
touching the Gmail I/O or apply paths.

A message only becomes trash-eligible when promotional evidence wins against
the protection rules, so these lists are policy inputs, not throwaway search
terms.
"""

from __future__ import annotations


READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
MAIL_SCOPE = "https://mail.google.com/"
DEFAULT_QUERY = "before:2025/12/30 -in:trash"
ROOT_LABEL = "Sorter"

# Catch-all categories that describe "we did not learn anything useful" rather
# than a real filing decision. They are tracked for the dashboard but never
# turned into an applied Gmail label.
NON_LABEL_CATEGORIES = {"Review", "Updates"}

# Independent bulk-mail signals. Archive requires real evidence that a message
# is machine-sent bulk mail, not just a subject line that happens to score high.
BULK_MAIL_REASONS = {
    "gmail_category_promotions",
    "list_unsubscribe_header",
    "one_click_unsubscribe_header",
    "list_id_header",
    "bulk_or_list_precedence",
    "campaign_header",
    "body_unsubscribe_link",
}

AD_SUBJECT_KEYWORDS = [
    "sale",
    "deal",
    "deals",
    "discount",
    "promo",
    "promotion",
    "coupon",
    "offer",
    "limited time",
    "save ",
    "% off",
    "free shipping",
    "clearance",
    "flash sale",
    "black friday",
    "cyber monday",
    "newsletter",
    "new arrivals",
    "just dropped",
    "shop now",
    "last chance",
    "ends tonight",
    "exclusive",
]

AD_BODY_KEYWORDS = [
    "unsubscribe",
    "manage preferences",
    "email preferences",
    "view in browser",
    "view this email in your browser",
    "you are receiving this email because",
    "marketing email",
    "promotional email",
    "privacy policy",
]

AD_SENDER_KEYWORDS = ["newsletter", "marketing", "promo", "promotions", "offers", "deals"]

STRONG_PROMO_SUBJECT_PATTERNS = [
    r"\b\d{1,2}%\s*off\b",
    r"\b\d{1,2}\s*percent\s*off\b",
    r"\b(?:last chance|final hours|ends tonight|today only)\b",
    r"\b(?:flash sale|clearance|warehouse sale|summer sale|winter sale)\b",
    r"\b(?:black friday|cyber monday|boxing day)\b",
    r"\b(?:free shipping|free delivery)\b",
    r"\b(?:shop now|new arrivals|just dropped)\b",
]

PROMO_SENDER_LOCALPARTS = {
    "deals",
    "email",
    "hello",
    "info",
    "marketing",
    "newsletter",
    "newsletters",
    "offers",
    "promo",
    "promotions",
    "sales",
}

TRANSACTIONAL_KEYWORDS = [
    "2fa",
    "account alert",
    "appointment",
    "bank",
    "bill",
    "booking",
    "code",
    "confirm your email",
    "delivery",
    "document",
    "e-transfer",
    "invoice",
    "login",
    "mfa",
    "order",
    "password",
    "payment",
    "payroll",
    "receipt",
    "refund",
    "reset",
    "security",
    "shipment",
    "shipped",
    "statement",
    "tax",
    "ticket",
    "transaction",
    "verification",
    "verify",
]

IMPORTANT_LABELS = {"CATEGORY_PRIMARY", "STARRED", "IMPORTANT"}
# Protected categories are the hard stop list. If a message lands here, it can
# still be labeled for review, but it should not be archived or trashed.
PROTECTED_CATEGORIES = {
    "Account Security",
    "Finance",
    "Government Legal",
    "Health",
    "Insurance",
    "Priority Attachments",
    "Priority Immigration",
    "Priority Studies",
    "Receipts Orders",
    "Utilities",
    "Work School",
}

IMMIGRATION_KEYWORDS = [
    "immigration",
    "ircc",
    "cic",
    "visa",
    "work permit",
    "study permit",
    "permanent residence",
    "pr card",
    "express entry",
    "biometrics",
    "lawyer",
    "law firm",
    "legal counsel",
    "barrister",
    "solicitor",
    "marolia",
    "pinaz",
    "tiffani",
    "ronen",
    "raquel",
    "jemma",
    "jonalyn",
    "oskoii",
    "oskooii",
    "oskoui",
    "osgoode",
]

STUDIES_KEYWORDS = [
    "university",
    "college",
    "course",
    "class",
    "assignment",
    "tuition",
    "transcript",
    "diploma",
    "degree",
    "registrar",
    "student",
    "student record",
    "academic",
    "enrolment",
    "enrollment",
    "exam",
    "grade",
    "syllabus",
]

CATEGORY_RULES = [
    ("Priority Immigration", IMMIGRATION_KEYWORDS, []),
    ("Priority Studies", STUDIES_KEYWORDS, []),
    ("Finance", ["bank", "credit card", "debit", "statement", "payment", "payroll", "invoice", "tax", "cra", "irs", "etransfer", "e-transfer"], []),
    ("Receipts Orders", ["receipt", "order", "purchase", "shipment", "shipped", "delivered", "delivery", "tracking", "refund", "return"], []),
    ("Account Security", ["password", "reset", "verification", "verify", "security alert", "new login", "sign-in", "2fa", "mfa", "authentication", "code"], []),
    ("Travel", ["flight", "airline", "hotel", "reservation", "booking", "boarding", "itinerary", "rental car", "airbnb", "uber", "lyft"], []),
    ("Health", ["appointment", "clinic", "doctor", "dentist", "pharmacy", "prescription", "medical", "health", "uhn", "myuhn", "hospital", "specialist"], []),
    ("Government Legal", ["government", "court", "legal", "passport", "license", "notice"], []),
    ("Work School", ["meeting", "calendar", "deadline", "project", "coworker", "standup", "sprint", "school", "principal", "teacher"], []),
    ("Social", ["facebook", "instagram", "linkedin", "twitter", "x.com", "reddit", "discord", "snapchat", "tiktok"], []),
    ("Subscriptions", ["subscription", "renewal", "membership", "plan", "trial", "billing cycle"], []),
    ("Shopping", ["cart", "wishlist", "store", "shop", "retailer", "coupon", "discount"], []),
    ("Job Search", ["application", "resume", "interview", "recruiter", "job alert", "candidate", "position"], []),
    ("Housing", ["rent", "lease", "landlord", "tenant", "mortgage", "property", "apartment", "condo"], ["hospital", "clinic", "health network"]),
    ("Utilities", ["utility", "hydro", "internet", "mobile", "phone bill", "electricity", "gas bill"], []),
    ("Insurance", ["insurance", "policy", "claim", "premium", "coverage"], []),
    ("Crypto Finance Risk", ["crypto", "bitcoin", "ethereum", "wallet", "exchange", "trading"], []),
    ("Old Account Evidence", ["welcome to", "confirm your account", "activate your account", "account created", "username", "registered"], []),
]

# Precedence used to choose a single primary category. Protected/priority
# buckets win so the primary label reflects the safest, most specific filing
# decision instead of an arbitrary alphabetical pick.
PRIMARY_CATEGORY_PRECEDENCE = [
    "Priority Immigration",
    "Priority Studies",
    "Priority Attachments",
    "Account Security",
    "Finance",
    "Government Legal",
    "Health",
    "Insurance",
    "Receipts Orders",
    "Utilities",
    "Work School",
    "Travel",
    "Housing",
    "Job Search",
    "Subscriptions",
    "Crypto Finance Risk",
    "Old Account Evidence",
    "Shopping",
    "Social",
    "Forums",
    "Ads Promotions",
    "Newsletters Bulk",
    "Updates",
    "Review",
]


# Default scoring weights and thresholds. These can be overridden per-run or via
# config/policy.yaml without editing code.
SCORE_WEIGHTS = {
    "sender": 8,
    "subject": 10,
    "snippet": 12,
}
SCORE_CAPS = {
    "sender": 25,
    "subject": 35,
    "snippet": 30,
    "subject_pattern": 35,
}
DEFAULTS = {
    "ad_threshold": 65,
    "archive_threshold": 65,
    "trash_threshold": 90,
    "pre_2020_trash_threshold": 75,
}
