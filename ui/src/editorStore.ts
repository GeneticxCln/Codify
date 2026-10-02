/**
 * The window's one set of editor buffers, wired to the engine.
 *
 * `editorBuffers.ts` is the store and knows nothing about the network, so tests give it a fake disk. This is the one place it
 * is handed the real one, and the one place the app imports it from.
 */
import { createEditorBuffers } from "./editorBuffers";
import { readWorkspaceFile, saveWorkspaceFile } from "./api";

export const editorBuffers = createEditorBuffers({ read: readWorkspaceFile, save: saveWorkspaceFile });
