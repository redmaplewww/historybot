"""Chinese text constants for the repair layer.

This module intentionally stores CJK text as ASCII unicode escapes so the
repair code is safe even when a shell or editor mangles non-ASCII input.
"""

ENTITY_TYPE_MAP = {
    "person": "person", "\u4eba\u7269": "person", "person_or_group": "person",
    "person_or_title": "person", "per": "person", "\u4eba": "person",
    "\u4eba\u7269\u7fa4\u4f53": "group", "person_group": "group", "group": "group",
    "\u7fa4\u4f53": "group", "organization": "group", "org": "group",
    "\u519b\u961f": "group", "\u90e8\u961f": "group",
    "official_title": "office", "official_position": "office", "office": "office",
    "\u5b98\u804c": "office", "title": "office", "\u7235\u4f4d": "office",
    "\u804c\u5b98": "office",
    "place": "place", "location": "place", "\u5730\u70b9": "place",
    "\u5730\u540d": "place", "loc": "place", "\u884c\u653f\u533a\u5212": "place",
    "\u884c\u653f\u533a": "place", "\u5730\u7406": "place",
    "polity": "group", "political_entity": "group", "\u653f\u6743": "group",
    "\u56fd\u5bb6": "group", "\u653f\u6cbb\u4f53": "group", "\u65cf\u7fa4": "group",
    "\u4eba\u7fa4": "group",
    "work": "work", "\u8457\u4f5c": "work", "\u4e66": "work", "\u7bc7\u7ae0": "work",
    "object": "object", "\u7269\u54c1": "object", "\u5668\u7269": "object",
    "\u7269\u4ef6": "object",
    "event": "event", "\u4e8b\u4ef6": "event", "\u4e8b\u4ef6\u7c7b\u578b": "event",
    "time": "time", "\u65f6\u95f4": "time", "date": "time",
    "concept": "concept", "\u5236\u5ea6": "concept", "\u5236\u5ea6\u540d": "concept",
    "\u6982\u5ff5": "concept", "\u653f\u7b56": "concept", "\u672a\u77e5": "concept",
    "unknown": "concept",
}

PUNCTUATION_TRANSLATION = str.maketrans(
    "\uff0c\u3002\u3001\uff1b\uff1a\uff1f\uff01\u201c\u201d\u2018\u2019\uff08\uff09\u300a\u300b",
    ",.;:?!\"''()<>[]",
)

IDENTITY_RELATIONS = {"same_as", "same_person", "\u540c\u4e00\u4eba", "\u540c\u6307", "\u6307\u4ee3", "identity"}

ALIAS_RELATIONS = {
    "\u5b57", "\u540d", "\u53f7", "\u8c25\u53f7", "\u8c25", "\u522b\u540d",
    "\u522b\u79f0", "\u7b80\u79f0", "\u7701\u79f0", "\u79f0\u8c13",
    "\u66fe\u7528\u540d", "abbreviation", "alias", "short_name", "title_of",
}

SANGUO_SOURCE_PATTERN = "\u4e09\u56fd\u5fd7\u00b7\u5377%"

ERA_STARTS = {
    "\u4e2d\u5e73": 184, "\u521d\u5e73": 190, "\u8208\u5e73": 194,
    "\u5efa\u5b89": 196, "\u5ef6\u5eb7": 220, "\u9ec3\u521d": 220,
    "\u592a\u548c": 227, "\u9752\u9f8d": 233, "\u666f\u521d": 237,
    "\u6b63\u59cb": 240, "\u5609\u5e73": 249, "\u6b63\u5143": 254,
    "\u7518\u9732": 256, "\u666f\u5143": 260, "\u54b8\u7199": 264,
    "\u7ae0\u6b66": 221, "\u5efa\u8208": 223, "\u5ef6\u7199": 238,
    "\u666f\u8000": 258, "\u708e\u8208": 263, "\u9ec3\u6b66": 222,
    "\u9ec3\u9f8d": 229, "\u5609\u79be": 232, "\u8d64\u70cf": 238,
    "\u592a\u5143": 251, "\u795e\u51e4": 252, "\u4e94\u9cf3": 254,
    "\u592a\u5e73": 256, "\u6c38\u5b89": 258, "\u5143\u8208": 264,
    "\u5b9d\u9f0e": 266, "\u5efa\u8861": 269, "\u51e4\u51f0": 272,
    "\u5929\u518a": 275, "\u5929\u73ba": 276, "\u5929\u7d00": 277,
    "\u7199\u5e73": 172, "\u5149\u548c": 178, "\u5149\u71b9": 189,
    "\u662d\u5b81": 189, "\u6c38\u6f22": 189, "\u5efa\u5b81": 168,
}


TEMPORAL_SYSTEM = (
    "\u4f60\u662f\u4e2d\u56fd\u53e4\u4ee3\u53f2\u6599\u65f6\u95f4\u8003\u636e\u5458\u3002"
    "\u53ea\u80fd\u4f9d\u636e\u63d0\u4f9b\u7684\u4e0a\u4e0b\u6587\u63a8\u5b9a\u65f6\u95f4\uff0c"
    "\u4e0d\u80fd\u4f7f\u7528\u5916\u90e8\u4e09\u56fd\u77e5\u8bc6\u3002"
    "\u524d\u540e\u6587\u4e2d\u7684\u7eaa\u5e74\u53ef\u4ee5\u4f20\u9012\u7ed9\u540c\u4e00"
    "\u6bb5\u843d\u4e2d\u7528\u201c\u662f\u5c81\u201d\u201c\u9042\u201d\u201c\u660e\u5e74\u201d\u201c\u521d\u201d"
    "\u7b49\u627f\u63a5\u6216\u8ffd\u8ff0\u7684\u4e8b\u4ef6\u3002"
    "\u5c06\u76f8\u5bf9\u65f6\u95f4\u6216\u7701\u7565\u65f6\u95f4\u89e3\u6790\u4e3a\u7edd\u5bf9\u65f6\u95f4\uff1b"
    "\u8f93\u51fa\u516c\u5143\u5e74\u3002\u5e74\u53f7\u7eaa\u5e74\u8f6c\u6362\u65f6\u4fdd\u7559"
    "\u5e74\u53f7\u4e0e\u5e74\u6570\u3002\u4e0d\u786e\u5b9a\u65f6\u5fc5\u987b\u7ed9 "
    "time_precision=uncertain\uff0c\u4e0d\u8981\u7f16\u9020\u5e74\u4efd\u3002"
)

TEMPORAL_USER_TEMPLATE = (
    "\u6309\u5377\u5185\u539f\u6587\u987a\u5e8f\u63d0\u4f9b\u4e0a\u4e0b\u6587\uff1b"
    "\u6700\u540e\u4e00\u4e2a passage_id \u662f\u5f53\u524d\u89e3\u6790\u5bf9\u8c61\u3002\n"
    "\u4e0a\u4e0b\u6587\u539f\u6587\uff1a\n{source_context}\n\n"
    "\u9700\u8981\u89e3\u6790\u7684\u76ee\u6807\uff1a\n{target_context}\n\n"
    "\u5bf9\u6bcf\u4e2a\u76ee\u6807\u8fd4\u56de references\u3002"
    "\u5b57\u6bb5\u542b\u4e49\uff1ayear=\u516c\u5143\u5e74\uff1b"
    "era_name/era_year=\u5e74\u53f7\u7eaa\u5e74\uff1b"
    "month/day \u4ec5\u5728\u6709\u6708\u65e5\u65f6\u586b\u5199\uff1b"
    "time_from/time_to \u53ef\u76f4\u63a5\u586b ISO \u5b57\u7b26\u4e32\u6216\u7559\u7a7a\uff1b"
    "time_precision \u53d6 day/month/year/range/decade/era_only/uncertain\uff1b"
    "context_basis \u5fc5\u987b\u8bf4\u660e\u53d6\u54ea\u4e2a\u4e0a\u4e0b\u6587\u3002"
)

TEMPORAL_USER_ANCHOR_TEMPLATE = (
    "\u6309\u5377\u5185\u539f\u6587\u987a\u5e8f\u63d0\u4f9b\u4e0a\u4e0b\u6587\uff1b"
    "\u6700\u540e\u4e00\u4e2a passage_id \u662f\u5f53\u524d\u89e3\u6790\u5bf9\u8c61\u3002\n\n"
    "\u524d\u6587\u9605\u8bfb\u6458\u8981\uff1a\n{reading_summaries}\n\n"
    "\u524d\u6587\u951a\u70b9\u539f\u6587\uff1a\n{anchor_context}\n\n"
    "\u4e0a\u4e0b\u6587\u539f\u6587\uff1a\n{source_context}\n\n"
    "\u9700\u8981\u89e3\u6790\u7684\u76ee\u6807\uff1a\n{target_context}\n\n"
    "\u5e74\u53f7\u7eaa\u5e74\u5bf9\u7167\u5de5\u5177\uff08\u4ec5\u4f9b\u6838\u5bf9\uff0c"
    "\u4e0d\u80fd\u66ff\u4ee3\u4e0a\u4e0b\u6587\u5224\u65ad\uff09\uff1a\n{era_table}\n\n"
    "\u5bf9\u6bcf\u4e2a\u76ee\u6807\u8fd4\u56de references\u3002"
    "\u5b57\u6bb5\u542b\u4e49\uff1ayear=\u516c\u5143\u5e74\uff1b"
    "era_name/era_year=\u5e74\u53f7\u7eaa\u5e74\uff1b"
    "month/day \u4ec5\u5728\u6709\u6708\u65e5\u65f6\u586b\u5199\uff1b"
    "time_from/time_to \u53ef\u76f4\u63a5\u586b ISO \u5b57\u7b26\u4e32\u6216\u7559\u7a7a\uff1b"
    "time_precision \u53d6 day/month/year/range/decade/era_only/uncertain\uff1b"
    "context_basis \u5fc5\u987b\u5177\u4f53\u8bf4\u660e\u7531\u54ea\u6bb5\u524d\u6587\u3001"
    "\u5e74\u53f7\u3001\u6708\u6570\u6216\u201c\u662f\u5c81/\u660e\u5e74/\u521d\u201d"
    "\u7b49\u5173\u7cfb\u5f97\u51fa\u65f6\u95f4\u3002"
)
