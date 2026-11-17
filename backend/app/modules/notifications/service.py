"""Notification service — business logic for preferences and multi-channel dispatch."""

import uuid
from typing import Annotated, Protocol

import structlog
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    EmailDispatchError,
    InactiveUserError,
    TransactionalOptOutError,
    UrgentHelpError,
    UserNotFoundError,
)
from app.core.validators import CanonicalPhone
from app.db.session import get_db
from app.modules.notifications.dispatcher import (
    DispatchPayload,
    InlineDispatcher,
    NotificationDispatcher,
)
from app.modules.notifications.dispatcher import (
    mask_email as _mask_email,
)
from app.modules.notifications.email import EmailProvider, get_email_provider
from app.modules.notifications.model import NotificationPreference
from app.modules.notifications.push import PushProvider, get_push_provider
from app.modules.notifications.registry import (
    DEFAULT_PREFERENCES,
    EVENT_REGISTRY,
    NON_OPT_OUT_CLASSES,
    VALID_CHANNELS,
    get_event_class,
    validate_channel,
)
from app.modules.notifications.schemas import (
    DeliveryResult,
    NotificationContent,
    PreferenceItem,
    PreferencesResponse,
    UpdatePreferencesRequest,
)
from app.modules.notifications.sms import SMSProvider, get_sms_provider
from app.modules.users.model import User
from fastapi import BackgroundTasks, Depends
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


class PreferenceSink(Protocol):
    """Observation/external-signal hook for notification preference suppression."""

    async def mark_channel_suppressed(
        self,
        user_id: uuid.UUID,
        channel: str,
        reason: str,
    ) -> None:
        """Record an external suppression signal (e.g. ESP bounce or SMS DND webhook)."""
        ...


class NoOpPreferenceSink:
    """M1 no-op PreferenceSink stub logging external suppression signals."""

    async def mark_channel_suppressed(
        self,
        user_id: uuid.UUID,
        channel: str,
        reason: str,
    ) -> None:
        logger.info(
            "preference_sink_noop",
            user_id=str(user_id),
            channel=channel,
            reason=reason,
        )


def get_background_tasks(background_tasks: BackgroundTasks) -> BackgroundTasks:
    """Expose FastAPI BackgroundTasks as a sub-dependency so optional callers keep None."""
    return background_tasks


class NotificationService:
    """Encapsulates notification preferences and multi-channel dispatch workflows."""

    def __init__(
        self,
        sms_provider: SMSProvider | None = None,
        email_provider: EmailProvider | None = None,
        settings: Settings | None = None,
        push_provider: PushProvider | None = None,
        db: AsyncSession | None = None,
        dispatcher: NotificationDispatcher | None = None,
        preference_sink: PreferenceSink | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._sms_provider = sms_provider or get_sms_provider(self._settings)
        self._email_provider = email_provider or get_email_provider(self._settings)
        self._push_provider = push_provider or get_push_provider(self._settings)
        self._db = db
        self._dispatcher: NotificationDispatcher = dispatcher or InlineDispatcher(
            sms_provider=self._sms_provider,
            email_provider=self._email_provider,
            push_provider=self._push_provider,
        )
        self._preference_sink: PreferenceSink = preference_sink or NoOpPreferenceSink()

    def _require_db(self) -> AsyncSession:
        if self._db is None:
            raise UrgentHelpError(
                code="NOTIFICATION_DB_REQUIRED",
                detail="Database session is required for notification preference operations.",
            )
        return self._db

    async def _get_active_user(self, db: AsyncSession, user_id: uuid.UUID) -> User:
        user = await db.get(User, user_id)
        if user is None:
            raise UserNotFoundError(context={"user_id": str(user_id)})
        if not user.is_active:
            raise InactiveUserError(context={"user_id": str(user_id)})
        return user

    # ─── Preference Management ────────────────────────────────────────────────

    async def get_preferences(self, user_id: uuid.UUID) -> PreferencesResponse:
        """Return materialized VALID_CHANNELS x EVENT_REGISTRY preferences without DB writes."""
        db = self._require_db()
        await self._get_active_user(db, user_id)

        stmt = (
            select(NotificationPreference)
            .where(NotificationPreference.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        overrides: dict[tuple[str, str], bool] = {
            (row.channel, row.event_type): row.enabled for row in rows
        }

        items = [
            PreferenceItem(
                channel=channel,
                event_type=event_type,
                enabled=(
                    True
                    if event_class in NON_OPT_OUT_CLASSES
                    else overrides.get(
                        (channel, event_type),
                        DEFAULT_PREFERENCES[(channel, event_type)],
                    )
                ),
            )
            for channel in VALID_CHANNELS
            for event_type, event_class in EVENT_REGISTRY.items()
        ]
        return PreferencesResponse(preferences=items)

    async def upsert_preferences(
        self,
        user_id: uuid.UUID,
        payload: UpdatePreferencesRequest,
    ) -> PreferencesResponse:
        """MERGE preference updates in one transaction and return materialized preferences."""
        db = self._require_db()
        await self._get_active_user(db, user_id)

        deduped: dict[tuple[str, str], bool] = {}
        for item in payload.preferences:
            validate_channel(item.channel)
            event_class = get_event_class(item.event_type)
            if event_class in NON_OPT_OUT_CLASSES:
                if not item.enabled:
                    raise TransactionalOptOutError(
                        context={"channel": item.channel, "event_type": item.event_type}
                    )
                continue
            deduped[(item.channel, item.event_type)] = item.enabled

        if deduped:
            rows_to_upsert = [
                {
                    "id": uuid.uuid4(),
                    "user_id": user_id,
                    "channel": channel,
                    "event_type": event_type,
                    "enabled": enabled,
                }
                for (channel, event_type), enabled in deduped.items()
            ]
            insert_stmt = pg_insert(NotificationPreference).values(rows_to_upsert)
            upsert_stmt = insert_stmt.on_conflict_do_update(
                constraint="uq_notification_pref",
                set_={
                    "enabled": insert_stmt.excluded.enabled,
                    "updated_at": func.now(),
                },
            )
            try:
                await db.execute(upsert_stmt)
                await db.commit()
            except Exception:
                await db.rollback()
                raise

        return await self.get_preferences(user_id)

    # ─── Pre-Dispatch Gating & Unified Dispatch ──────────────────────────────

    async def _check_preference_with_reason(
        self,
        user_id: uuid.UUID,
        channel: str,
        event_type: str,
    ) -> tuple[bool, str | None]:
        """Check whether (user_id, channel, event_type) is enabled inside a DB savepoint."""
        validate_channel(channel)
        event_class = get_event_class(event_type)
        fail_open = event_class in NON_OPT_OUT_CLASSES

        if self._db is None:
            logger.warning(
                "preference_lookup_no_db",
                user_id=str(user_id),
                channel=channel,
                event_type=event_type,
                fail_open=fail_open,
            )
            return fail_open, (None if fail_open else "no_db_session")

        try:
            async with self._db.begin_nested():
                stmt = select(NotificationPreference.enabled).where(
                    NotificationPreference.user_id == user_id,
                    NotificationPreference.channel == channel,
                    NotificationPreference.event_type == event_type,
                )
                result = await self._db.execute(stmt)
                stored = result.scalar_one_or_none()
        except Exception as exc:
            logger.warning(
                "preference_lookup_failed",
                user_id=str(user_id),
                channel=channel,
                event_type=event_type,
                event_class=event_class.value,
                fail_open=fail_open,
                error=str(exc),
            )
            return fail_open, (None if fail_open else "preference_lookup_failed")

        if fail_open:
            return True, None
        enabled = bool(stored) if stored is not None else DEFAULT_PREFERENCES[(channel, event_type)]
        return enabled, (None if enabled else "preference_disabled")

    async def _check_preference(
        self,
        user_id: uuid.UUID,
        channel: str,
        event_type: str,
    ) -> bool:
        """Check whether (user_id, channel, event_type) is enabled inside a DB savepoint."""
        allowed, _ = await self._check_preference_with_reason(
            user_id=user_id,
            channel=channel,
            event_type=event_type,
        )
        return allowed

    async def _resolve_contact_with_reason(
        self,
        user_id: uuid.UUID,
        channel: str,
        *,
        bypass_readiness: bool = False,
    ) -> tuple[str | None, str | None]:
        validate_channel(channel)
        if self._db is None:
            return None, "no_db_session"

        try:
            async with self._db.begin_nested():
                user = await self._db.get(User, user_id)
        except Exception as exc:
            logger.warning(
                "user_lookup_failed",
                user_id=str(user_id),
                channel=channel,
                error=str(exc),
            )
            return None, "db_lookup_failed"

        if user is None:
            return None, "user_not_found"
        if not bypass_readiness and not user.is_active:
            return None, "user_inactive"

        if channel == "email":
            if not user.email:
                return None, "missing_email"
            if not bypass_readiness and user.email_verified_at is None:
                return None, "email_unverified"
            return user.email, None

        if channel == "sms":
            if not user.phone:
                return None, "missing_phone"
            if not bypass_readiness and user.phone_verified_at is None:
                return None, "phone_unverified"
            return user.phone, None

        raw_token: object = getattr(user, "fcm_token", None)
        if not isinstance(raw_token, str) or not raw_token:
            return None, "no_device_token"
        return raw_token, None

    async def resolve_contact(
        self,
        user_id: uuid.UUID,
        channel: str,
        *,
        bypass_readiness: bool = False,
    ) -> str | None:
        """Resolve a deliverable recipient identifier for (user_id, channel), or None if unready."""
        recipient, _ = await self._resolve_contact_with_reason(
            user_id=user_id,
            channel=channel,
            bypass_readiness=bypass_readiness,
        )
        return recipient

    async def send_notification(
        self,
        user_id: uuid.UUID,
        channel: str,
        event_type: str,
        content: NotificationContent,
    ) -> DeliveryResult:
        """Evaluate preferences and contact readiness, then dispatch a DB-free payload."""
        validate_channel(channel)
        event_class = get_event_class(event_type)

        allowed, pref_reason = await self._check_preference_with_reason(
            user_id=user_id,
            channel=channel,
            event_type=event_type,
        )
        if not allowed:
            logger.info(
                "notification_suppressed",
                status="suppressed",
                user_id=str(user_id),
                channel=channel,
                event_type=event_type,
                reason=pref_reason,
            )
            return DeliveryResult(status="suppressed", reason=pref_reason)

        bypass_readiness = event_class in NON_OPT_OUT_CLASSES
        recipient, reason = await self._resolve_contact_with_reason(
            user_id=user_id,
            channel=channel,
            bypass_readiness=bypass_readiness,
        )
        if recipient is None:
            logger.info(
                "notification_suppressed",
                status="suppressed",
                user_id=str(user_id),
                channel=channel,
                event_type=event_type,
                reason=reason,
            )
            return DeliveryResult(status="suppressed", reason=reason)

        payload = DispatchPayload(
            user_id=user_id,
            channel=channel,
            event_type=event_type,
            event_class=event_class,
            recipient=recipient,
            content=content,
        )
        return await self._dispatcher.dispatch(payload)

    # ─── Legacy Ungated Transactional Methods ─────────────────────────────────

    async def send_otp_sms(
        self,
        to: CanonicalPhone,
        otp: str,
        expire_minutes: int,
    ) -> None:
        """Format and dispatch OTP SMS to the recipient."""
        message = f"Your UrgentHelp verification code is {otp}. Valid for {expire_minutes} minutes."
        await self._sms_provider.send_sms(to=to, message=message)
        logger.info("otp_sms_sent", phone=f"...{to[-4:]}")

    async def send_verification_email(
        self,
        to: str,
        otp: str,
        expire_hours: int,
    ) -> None:
        """Format and dispatch email verification code to the recipient."""
        subject = "Verify your UrgentHelp email address"
        body = f"Your UrgentHelp verification code is {otp}. Valid for {expire_hours} hours."
        logged_email = _mask_email(to) if self._settings.is_production else to
        try:
            await self._email_provider.send_email(
                to=to,
                subject=subject,
                body=body,
            )
            logger.info("email_verification_sent", email=logged_email)
        except EmailDispatchError:
            raise
        except Exception as exc:
            logger.error("email_dispatch_failed", email=logged_email, error=str(exc))
            raise EmailDispatchError(
                detail="Failed to dispatch verification email. Please try again.",
                context={"email": logged_email, "error": str(exc)},
            ) from exc

    async def send_password_reset_email(
        self,
        to: str,
        token: str,
        expire_minutes: int,
    ) -> None:
        """Format and dispatch password reset link/token to the recipient."""
        subject = "Reset your UrgentHelp password"
        body = (
            f"Use the following token to reset your password: {token}. "
            f"This link/token is valid for {expire_minutes} minutes."
        )
        logged_email = _mask_email(to) if self._settings.is_production else to
        try:
            await self._email_provider.send_email(
                to=to,
                subject=subject,
                body=body,
            )
            logger.info("password_reset_email_sent", email=logged_email)
        except EmailDispatchError:
            raise
        except Exception as exc:
            logger.error("password_reset_email_failed", email=logged_email, error=str(exc))
            raise EmailDispatchError(
                detail="Failed to dispatch password reset email. Please try again.",
                context={"email": logged_email, "error": str(exc)},
            ) from exc

    async def send_password_reset_sms(
        self,
        to: CanonicalPhone,
        otp: str,
        expire_minutes: int,
    ) -> None:
        """Format and dispatch password reset SMS OTP to the recipient."""
        message = (
            f"Your UrgentHelp password reset code is {otp}. Valid for {expire_minutes} minutes."
        )
        await self._sms_provider.send_sms(to=to, message=message)
        logger.info("password_reset_sms_sent", phone=f"...{to[-4:]}")


def get_notification_service(
    db: Annotated[AsyncSession | None, Depends(get_db)] = None,
    settings: Annotated[Settings | None, Depends(get_settings)] = None,
    background_tasks: Annotated[BackgroundTasks | None, Depends(get_background_tasks)] = None,
) -> NotificationService:
    """FastAPI dependency factory wiring NotificationService with DB and InlineDispatcher."""
    resolved_settings = settings or get_settings()
    sms_provider = get_sms_provider(resolved_settings)
    email_provider = get_email_provider(resolved_settings)
    push_provider = get_push_provider(resolved_settings)
    dispatcher = InlineDispatcher(
        sms_provider=sms_provider,
        email_provider=email_provider,
        push_provider=push_provider,
        background_tasks=background_tasks,
    )
    return NotificationService(
        sms_provider=sms_provider,
        email_provider=email_provider,
        settings=resolved_settings,
        push_provider=push_provider,
        db=db,
        dispatcher=dispatcher,
    )
