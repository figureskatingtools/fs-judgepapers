/**
 * In-app instructions: small question-mark triggers that reveal a popover
 * explaining which Figure Skating Manager exports to upload.
 *
 * The file list is built from the validate.ts constants so the help copy
 * cannot drift away from what validation actually requires.
 */
import { COMMON_FILES, ISU_ONLY_FILES, CATEGORY_GENERAL_FILE } from './validate';

/**
 * Markup for one question-mark trigger plus its popover.
 *
 * @param popoverId  unique id, referenced by the trigger's aria-controls
 * @param label      accessible name for the trigger button
 * @param popoverHtml  popover body (static markup — no escaping needed)
 * @param modifier   extra class on the wrapper, e.g. 'help-wrap--right'
 */
export function renderHelpTrigger(
    popoverId: string,
    label: string,
    popoverHtml: string,
    modifier = ''
): string {
    return `
<span class="help-wrap ${modifier}">
  <button type="button" class="help-trigger" aria-expanded="false"
          aria-controls="${popoverId}" aria-label="${label}">?</button>
  <div class="help-popover" role="note" id="${popoverId}">${popoverHtml}</div>
</span>`;
}

/**
 * Wire up popover open/close once, via delegated listeners on document.
 * Popovers also open on hover/focus via CSS; the click toggle is for
 * touch devices and for keeping a popover open while reading it.
 */
export function initHelp(): void {
    const closeAll = () => {
        document.querySelectorAll('.help-wrap.is-open').forEach(wrap => {
            wrap.classList.remove('is-open');
            wrap.querySelector('.help-trigger')?.setAttribute('aria-expanded', 'false');
        });
    };

    document.addEventListener('click', (e) => {
        const target = e.target as HTMLElement | null;
        if (!target) return;

        const trigger = target.closest('.help-trigger');
        if (trigger) {
            const wrap = trigger.closest('.help-wrap');
            if (wrap) {
                const willOpen = !wrap.classList.contains('is-open');
                closeAll();
                if (willOpen) {
                    wrap.classList.add('is-open');
                    trigger.setAttribute('aria-expanded', 'true');
                }
            }
            // Don't let the click reach the upload area (which opens a file dialog)
            e.stopPropagation();
            return;
        }

        // Clicking inside an open popover must not dismiss it — the user may
        // be selecting a filename to copy.
        if (target.closest('.help-popover')) {
            e.stopPropagation();
            return;
        }

        closeAll();
    });

    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        const open = document.querySelector('.help-wrap.is-open');
        if (!open) return;
        const trigger = open.querySelector<HTMLButtonElement>('.help-trigger');
        closeAll();
        trigger?.focus();
    });
}

/** Popover body listing the FSM exports required for each segment/category. */
export function filesHelpHtml(): string {
    const perSegment = [...COMMON_FILES, ...ISU_ONLY_FILES]
        .map(f => f === 'TechnicalSpecialistSheet'
            ? 'TechnicalSpecialistSheet1, 2… (each specialist)'
            : f)
        .map(f => `<li>${f}</li>`)
        .join('');

    return `
<h4 class="help-popover-title">Files to export from FSM</h4>
<p class="help-popover-sub">For every segment</p>
<ul class="help-file-list">${perSegment}</ul>
<p class="help-popover-sub">Once per category</p>
<ul class="help-file-list"><li>${CATEGORY_GENERAL_FILE}</li></ul>
<div class="help-warning">
  <span class="help-warning-label">Common mistake</span>
  <p class="help-do">Export <strong>PlannedProgramContent</strong></p>
  <p class="help-dont">not <strong>PlannedProgramContentChecklist</strong> &mdash; the Checklist is a different FSM export and will not work.</p>
</div>
<p class="help-popover-note">MUPI categories need only the first four files. CompetitionSchedule.pdf is optional.</p>`;
}
