import React, { useEffect, useRef, useState } from "react";
import { AlertTriangle, Bell, CheckCheck, CheckCircle2, Info, Trash2, X, XCircle } from "lucide-react";
import { relativeTime, unreadCount, type AppNotification, type NotificationTone } from "../notifications";
import { IconButton } from "./ui/IconButton";
import { toneText } from "./ui/Badge";

/**
 * The notifications drawer: the list, newest first, in the same place the other two drawers sit.
 *
 * A drawer and not a popover, for the reason the others are: the native browser webview paints above
 * every DOM overlay (`docs/09` §8.1), so a dropdown from the header would be drawn *under* a browser
 * tab's page. A flex sibling of the centre column shrinks it instead, and cannot be covered.
 *
 * Opening it is reading it: everything is marked read on open (and as new items arrive while it stays
 * open), so the header's count clears at once and survives a restart. The dot on a row is for the ones
 * that were new when the drawer was opened, which the drawer remembers for as long as it is open, so
 * the person can still see what they are looking at that they had not seen.
 */

const ICONS: Record<NotificationTone, React.ComponentType<{ className?: string; "aria-hidden"?: "true" }>> = {
  success: CheckCircle2,
  danger: XCircle,
  warning: AlertTriangle,
  info: Info,
};

/** A minute is the finest the labels speak in, so a minute is how often they need redrawing. */
const REFRESH_MS = 60_000;

export const NotificationsDrawer: React.FC<{
  items: readonly AppNotification[];
  onMarkAllRead: () => void;
  onClear: () => void;
  onClose: () => void;
  /** A row with somewhere to go was pressed. */
  onOpenItem: (item: AppNotification) => void;
}> = ({ items, onMarkAllRead, onClear, onClose, onOpenItem }) => {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), REFRESH_MS);
    return () => clearInterval(timer);
  }, []);

  // The ids that were unread when the drawer opened, plus any that arrive while it is open. A ref, grown
  // during render, because the point is that it must *not* shrink when everything is marked read below.
  const fresh = useRef<Set<string>>(new Set());
  for (const item of items) if (!item.read) fresh.current.add(item.id);

  const unread = unreadCount(items);
  useEffect(() => {
    if (unread > 0) onMarkAllRead();
  }, [unread, onMarkAllRead]);

  return (
    <aside
      aria-label="Notifications"
      className="w-80 max-w-[40%] shrink-0 bg-codify-surface border-l border-codify-border flex flex-col"
    >
      <div className="flex items-center justify-between px-4 py-3 border-b border-codify-border">
        <div className="flex items-center gap-2 text-sm font-semibold text-codify-primary">
          <Bell className="w-4 h-4 text-codify-info" aria-hidden="true" />
          Notifications
        </div>
        <div className="flex items-center gap-2">
          <IconButton label="Mark all notifications read" onClick={onMarkAllRead} disabled={unread === 0}>
            <CheckCheck className="w-3.5 h-3.5" />
          </IconButton>
          <IconButton label="Clear notifications" onClick={onClear} disabled={items.length === 0}>
            <Trash2 className="w-3.5 h-3.5" />
          </IconButton>
          <IconButton label="Close notifications" onClick={onClose}>
            <X className="w-4 h-4" />
          </IconButton>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto overflow-x-hidden p-2 space-y-1">
        {items.length === 0 ? (
          // What the drawer is for, in a sentence, because an empty list is the first thing anyone sees.
          <div className="flex flex-col items-center gap-2 px-4 py-10 text-center text-xs text-codify-muted">
            <Bell className="w-5 h-5" aria-hidden="true" />
            <p className="font-semibold text-codify-secondary">Nothing yet</p>
            <p className="leading-relaxed">
              Goals that finish or fail, plans waiting for your approval, engine connection changes and model
              list changes show up here.
            </p>
          </div>
        ) : (
          items.map((item) => {
            const Icon = ICONS[item.tone];
            const isNew = fresh.current.has(item.id);
            const body = (
              <>
                <Icon className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${toneText(item.tone)}`} aria-hidden="true" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-2">
                    <span className="min-w-0 break-words text-xs font-semibold text-codify-primary">{item.title}</span>
                    {isNew && (
                      <span
                        role="img"
                        aria-label="Unread"
                        className="h-1.5 w-1.5 shrink-0 self-center rounded-full bg-codify-accent"
                      />
                    )}
                    <span className="ml-auto shrink-0 whitespace-nowrap text-2xs text-codify-muted">
                      {relativeTime(item.at, now)}
                    </span>
                  </div>
                  {item.detail && (
                    <p className="mt-0.5 break-words text-2xs leading-relaxed text-codify-secondary">{item.detail}</p>
                  )}
                </div>
              </>
            );
            const row = "flex w-full items-start gap-2 rounded-lg border border-codify-border bg-codify-bg px-2.5 py-2 text-left";
            return item.target ? (
              <button
                key={item.id}
                type="button"
                onClick={() => onOpenItem(item)}
                className={`${row} cursor-pointer transition-colors hover:bg-codify-raised`}
              >
                {body}
              </button>
            ) : (
              <div key={item.id} className={row}>
                {body}
              </div>
            );
          })
        )}
      </div>
    </aside>
  );
};
