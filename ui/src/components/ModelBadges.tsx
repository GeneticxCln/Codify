import React from "react";
import { Badge } from "./ui/Badge";
import type { modelBadges } from "../modelSignals";

/**
 * What a model row says about the model, as badges.
 *
 * The three facts `modelBadges` returns used to be drawn as bare coloured words at every call site:
 * the roles a model is configured on in the *knowledge* hue (reserved for the CODIFY.md deliverable,
 * and nothing else), "last run" in blue, "not a chat model" in amber with no shape beside it. A row
 * with all three read as three unrelated phrases running into the model's name, and the warning
 * was told apart from its neighbours by colour alone.
 *
 * They are `Badge`s now, so they are the same shape as every other tag in the app and follow the
 * same rules:
 *
 * - **roles** are a tag, not a state: `neutral`, no glyph. "planner, critic" says where the model is
 *   used and nothing about whether that is good or bad.
 * - **last run** is `info`, no glyph: it is news, not a severity.
 * - **not a chat model** is `warning`, and `Badge` puts the triangle beside it, so the one badge
 *   that means "this will probably not work" is also the one with a shape.
 *
 * Shared by the composer's model menu and by the model field in Settings: both draw the same list
 * from the same signals, and two copies of the markup is how the two menus drifted apart.
 */
export const ModelBadges: React.FC<{ badges: ReturnType<typeof modelBadges> }> = ({ badges }) => (
  <>
    {badges.roles ? (
      <Badge tone="neutral" icon={false} title={badges.rolesTitle} className="max-w-[11rem]">
        <span className="truncate">{badges.roles}</span>
      </Badge>
    ) : null}
    {badges.lastRun ? (
      <Badge tone="info" icon={false} title={badges.lastRunTitle}>
        last run
      </Badge>
    ) : null}
    {badges.notChat ? (
      <Badge tone="warning" title="The provider says this model does not support chat">
        not a chat model
      </Badge>
    ) : null}
  </>
);
