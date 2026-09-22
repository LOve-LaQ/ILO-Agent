# Services - 业务服务层
"""跨模块的业务编排（不依赖 FastAPI 路由，供路由与调度器共用）。"""

from src.services.activity_service import (
    count_activities,
    list_activities,
    log_activity,
)
from src.services.bookmark_service import (
    BookmarkError,
    add_bookmark,
    count_bookmarks,
    get_bookmarked_ids,
    is_bookmarked,
    list_bookmarks,
    remove_bookmark,
)
from src.services.collection_service import (
    BATCH_TRIGGER_MANUAL,
    BATCH_TRIGGER_SCHEDULED,
    CollectResult,
    build_provenance,
    collect_items,
    finish_batch,
    get_collected_ids,
    get_provenance,
    is_collected,
    record_item,
    start_batch,
)
from src.services.interest_profile import (
    aggregate_signals,
    build_user_profile,
    top_candidates,
    weighted_mean_vector,
)
from src.services.learning_service import (
    append_messages,
    complete_session_record,
    count_messages_by_session,
    get_session,
    list_messages,
    list_sessions,
    load_session,
    session_stats,
    sync_state_from_context,
    upsert_session,
)

from src.services.recommend import (
    cosine_similarity,
    diversify_by_category,
    is_degenerate_vector,
    normalize_score,
    rank_candidates,
)

__all__ = [
    "BATCH_TRIGGER_MANUAL",
    "BATCH_TRIGGER_SCHEDULED",
    "BookmarkError",
    "CollectResult",
    "add_bookmark",
    "aggregate_signals",
    "append_messages",
    "build_provenance",
    "build_user_profile",
    "collect_items",
    "complete_session_record",
    "count_activities",
    "count_bookmarks",
    "count_messages_by_session",
    "cosine_similarity",
    "diversify_by_category",
    "finish_batch",
    "get_bookmarked_ids",
    "get_collected_ids",
    "get_provenance",
    "get_session",
    "is_bookmarked",
    "is_collected",
    "is_degenerate_vector",
    "list_activities",
    "list_bookmarks",
    "list_messages",
    "list_sessions",
    "load_session",
    "log_activity",
    "normalize_score",
    "rank_candidates",
    "record_item",
    "remove_bookmark",
    "session_stats",
    "start_batch",
    "sync_state_from_context",
    "top_candidates",
    "upsert_session",
    "weighted_mean_vector",
]
