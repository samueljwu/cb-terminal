import colorsys
import json
import re
import tempfile
import unittest
from pathlib import Path

from cb_terminal.web.server import (
    CONTRACT_FIELD_CHOICES,
    CONTRACT_FIELD_LABELS,
    PROJECT_ROOT,
    generate_valuation_market_history_payload,
    market_generation_readiness_payload,
    render_dashboard_html,
)


class DashboardLayoutTests(unittest.TestCase):
    def test_valuation_history_build_requires_approved_terms(self):
        reviewed_path = PROJECT_ROOT / "data" / "contracts" / "XS3442802230_contract.json"
        raw = json.loads(reviewed_path.read_text(encoding="utf-8"))
        raw["status"] = "needs_review"
        raw.setdefault("source_review", {})["review_status"] = "needs_human_review"
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".json",
            prefix="_test_unapproved_",
            dir=reviewed_path.parent,
            delete=False,
        ) as handle:
            json.dump(raw, handle)
            temporary_path = Path(handle.name)
        try:
            readiness = market_generation_readiness_payload({"contract_path": str(temporary_path)})
            self.assertEqual(readiness["status"], "needs_terms_approval")
            self.assertFalse(readiness["terms_approved"])

            generated = generate_valuation_market_history_payload(
                {"contract_path": str(temporary_path), "confirm": True}
            )
            self.assertEqual(generated["status"], "needs_terms_approval")
            self.assertIn("Approve", generated["message"])
        finally:
            temporary_path.unlink(missing_ok=True)

    def test_terminal_navigation_and_instrument_shortcuts_drive_the_workbench(self):
        html = render_dashboard_html()
        self.assertIn('main class="workbench-layout"', html)
        self.assertIn('nav class="instrument-nav" aria-label="Instrument navigation"', html)
        self.assertIn('id="cb-command-input"', html)
        self.assertIn('class="command-shell"', html)
        self.assertIn('--text-command:clamp(', html)
        self.assertIn('--page-gutter:clamp(', html)
        self.assertIn('--radius:0px', html)
        self.assertIn('min-height:45px', html)
        self.assertIn('class="command-prompt">SECURITY</span>', html)
        self.assertIn('id="cb-command-ghost" class="command-input-ghost" aria-hidden="true"', html)
        self.assertIn('caret-color:var(--accent)', html)
        self.assertIn('--text-security-command:clamp(1.29rem, 1.23rem + .21vw, 1.47rem)', html)
        self.assertIn('.command-input-ghost { position:absolute; inset:6px 0 auto 0;', html)
        self.assertGreaterEqual(html.count('font-size:var(--text-security-command)'), 2)
        self.assertIn('button, input, select, textarea { font:inherit; }', html)
        self.assertIn('function updateCommandGhost()', html)
        self.assertIn('placeholder="ISIN / issuer / ticker / name"', html)
        self.assertIn('aria-label="Search by ISIN, issuer, ticker, or display name"', html)
        self.assertIn('id="cb-command-suggestions" class="command-autocomplete" role="listbox"', html)
        self.assertIn('id="selected-cb-identity"', html)
        self.assertIn('select id="cb-select" class="hidden-select"', html)
        self.assertIn('id="open-data-management"', html)
        self.assertIn('id="view-summary-data"', html)
        self.assertIn('data-tab="pm-view" aria-current="page">Summary</button>', html)
        self.assertIn('section id="tab-pm-view" class="tab-panel active"', html)
        self.assertIn('data-tab="data-management">Data</button>', html)
        self.assertIn('section id="tab-data-management" class="tab-panel"', html)
        self.assertIn('data-tab="assumptions">Assumptions</button>', html)
        self.assertIn('data-tab="nuke">Nuke</button>', html)
        self.assertIn('section id="tab-nuke" class="tab-panel"', html)
        self.assertIn('data-tab="help">Help</button>', html)
        self.assertIn('id="open-help"', html)
        primary_nav = html.split('<nav class="tab-bar" aria-label="Primary navigation">', 1)[1].split('</nav>', 1)[0]
        self.assertEqual(primary_nav.count('class="tab-button'), 5)
        self.assertLess(primary_nav.index('>Summary</button>'), primary_nav.index('>Data</button>'))
        self.assertLess(primary_nav.index('>Data</button>'), primary_nav.index('>Assumptions</button>'))
        self.assertLess(primary_nav.index('>Assumptions</button>'), primary_nav.index('>Nuke</button>'))
        self.assertLess(primary_nav.index('>Nuke</button>'), primary_nav.index('>Help</button>'))
        self.assertNotIn('data-tab="prospectus-intake"', primary_nav)
        self.assertNotIn('data-tab="data-intake"', primary_nav)
        self.assertNotIn('Overview', primary_nav)
        self.assertIn('<summary>Advanced views</summary>', html)
        advanced_nav = html.split('<nav class="tab-bar secondary-tab-bar"', 1)[1].split('</nav>', 1)[0]
        self.assertNotIn('data-tab="data-sources"', advanced_nav)
        self.assertNotIn('data-tab="help"', advanced_nav)
        self.assertIn("advancedSummary.textContent = advancedActive ? `Advanced: ${activeButton.textContent.trim()}` : 'Advanced views';", html)
        self.assertIn("if (advanced) advanced.open = false;", html)
        self.assertIn("if (btn.closest('.secondary-tabs')) focusDestination(btn.dataset.tab);", html)
        self.assertIn("if (active) btn.setAttribute('aria-current', 'page');", html)
        self.assertNotIn("btn.setAttribute('aria-selected'", html)
        self.assertIn("activateTab('assumptions');\n  focusDestination('assumptions');", html)
        self.assertIn('form id="nuke-form"', html)
        self.assertIn("fetch('/api/nuke'", html)
        self.assertIn('id="reset-nuke-anchor"', html)
        self.assertIn('function primeNukeFromPayload(payload, force=false)', html)
        guide_handler = html.split("document.querySelectorAll('[data-go-tab]')", 1)[1].split("document.querySelectorAll('[data-open-data-step]')", 1)[0]
        self.assertIn("if (btn.dataset.goSubtab) activateDataSubtab(btn.dataset.goSubtab);", guide_handler)
        self.assertIn("normalizeDataStep(btn.dataset.goSubtab) === 'match'", guide_handler)
        self.assertIn('section id="tab-assumptions" class="tab-panel"', html)
        self.assertIn('section class="panel controls-panel" aria-label="Pricing assumptions and controls"', html)
        self.assertIn('grid-template-columns:1fr', html)
        self.assertLess(html.index('id="cb-command-input"'), html.index('section class="plots-panel"'))
        self.assertLess(html.index('id="active-assumptions-strip"'), html.index('price-chart'))
        self.assertLess(html.index('id="edit-assumptions"'), html.index('price-chart'))
        self.assertGreater(html.index('Volatility (%) <input'), html.index('section id="tab-assumptions"'))

    def test_help_is_visible_concise_and_explains_each_file_type(self):
        html = render_dashboard_html()
        self.assertIn('section id="tab-help" class="tab-panel"', html)
        self.assertIn('<h2>Help</h2>', html)
        self.assertIn('PDF = bond terms', html)
        self.assertIn('CSV/XLSX = market prices', html)
        self.assertIn('<h3>Identifier status</h3>', html)
        self.assertIn('<b>ISIN assigned</b>', html)
        self.assertIn('<b>ISIN pending</b>', html)
        self.assertIn('Approval is a separate confirmation', html)
        self.assertIn('data-go-tab="data-management" data-go-subtab="upload"', html)
        self.assertIn('data-go-tab="data-management" data-go-subtab="match"', html)
        self.assertIn("window.location.pathname === '/help'", html)
        self.assertIn("focusDestination(btn.dataset.goTab, btn.dataset.goSubtab || '');", html)
        self.assertIn("function focusDestination(tabName, subtabName='')", html)
        self.assertIn("document.getElementById('data-subtab-' + dataStep)", html)
        for fluff in (
            'Turn source documents into a reviewable convertible-bond valuation.',
            'Looking for bond terms?',
            'A simple mental model',
            'class="overview-hero"',
            'class="journey-card"',
            'class="terms-callout"',
        ):
            self.assertNotIn(fluff, html)

    def test_pm_view_exposes_implied_volatility_failure_explanation(self):
        html = render_dashboard_html()
        self.assertIn('id="iv-diagnostics"', html)
        self.assertIn('function renderImpliedVolatilityDiagnostics(payload)', html)
        self.assertIn('Implied volatility unavailable', html)
        self.assertIn('No bond market price was supplied', html)
        self.assertIn('Target bond price is outside the solver range', html)
        self.assertIn('renderImpliedVolatilityDiagnostics(payload);', html)

    def test_plots_use_plain_matlab_style_svg_not_card_dashboard_chrome(self):
        html = render_dashboard_html()
        self.assertIn('class="matlab-plot"', html)
        self.assertIn('stroke-width="2"', html)
        self.assertIn('text-anchor="middle"', html)
        self.assertIn('Yield curve used as risk-free rate', html)
        self.assertIn('background:#000; border:1px solid #555', html)
        self.assertNotIn('box-shadow', html)
        self.assertNotIn('linear-gradient', html)

    def test_terminal_palette_is_neutral_amber_and_contains_no_blue_or_cyan(self):
        html = render_dashboard_html()
        self.assertIn('--bg:#000', html)
        self.assertIn('--surface:#050505', html)
        self.assertIn('--panel:#080808', html)
        self.assertIn('--accent:#ff9d00', html)
        self.assertIn('--radius:0px', html)
        self.assertIn('font-family:var(--mono)', html)
        self.assertIn('font-variant-numeric:tabular-nums', html)

        for color in re.findall(r'#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\b', html):
            digits = color[1:]
            if len(digits) == 3:
                digits = ''.join(character * 2 for character in digits)
            red, green, blue = (int(digits[index:index + 2], 16) / 255 for index in (0, 2, 4))
            hue, saturation, _ = colorsys.rgb_to_hsv(red, green, blue)
            self.assertFalse(
                0.48 <= hue <= 0.72 and saturation > 0.2,
                f'blue/cyan color remains in dashboard: {color}',
            )
        self.assertNotRegex(html.lower(), r'\b(?:rgb|rgba|hsl|hsla)\s*\(')
        self.assertNotRegex(html.lower(), r'\b(?:blue|cyan|navy|teal|aqua)\b')

    def test_plots_are_compact_enough_to_reduce_scroll_capture(self):
        html = render_dashboard_html()
        self.assertIn('svg.matlab-plot { width:100%; height:clamp(190px, 21vw, 260px); min-height:0;', html)
        self.assertIn('svg.matlab-plot.small-plot { height:clamp(150px, 16vw, 200px); }', html)
        self.assertIn('svg.matlab-plot.pm-plot { height:clamp(180px, 18vw, 230px); }', html)
        self.assertIn('svg.matlab-plot.pm-small-plot { height:clamp(140px, 14vw, 180px); }', html)
        self.assertIn('id="price-chart" class="matlab-plot pm-plot" viewBox="0 0 900 220"', html)
        self.assertIn('id="volatility-overlay-chart" class="matlab-plot pm-plot" viewBox="0 0 900 220"', html)
        self.assertIn('id="raw-quote-chart" class="matlab-plot" viewBox="0 0 900 240"', html)
        self.assertIn('id="valuation-cheapness-mini-chart" class="matlab-plot small-plot pm-small-plot" viewBox="0 0 900 160"', html)
        self.assertIn('id="rv-stock-chart" class="matlab-plot small-plot pm-small-plot" viewBox="0 0 900 160"', html)
        self.assertIn('function chartFrame(svg, fallbackWidth=900, fallbackHeight=240)', html)
        self.assertIn('function chartLayout(svg, lineCount=0)', html)
        self.assertIn('const legendColumns = frame.compact ? Math.min(2, Math.max(1, lineCount)) : 1;', html)
        self.assertIn('function drawChartEmptyState(svg, message)', html)
        self.assertIn("svg.dataset.emptyMessage = String(message || 'No chartable data');", html)
        self.assertIn('svg?.getBoundingClientRect?.()', html)
        self.assertIn("const xTickCount = Math.min(compact ? 3 : 5, rows.length);", html)
        self.assertIn('new ResizeObserver(entries =>', html)
        self.assertIn('scheduleResponsiveChartRender();', html)

    def test_chart_wheel_zoom_does_not_capture_normal_page_scroll(self):
        html = render_dashboard_html()
        self.assertIn('class="chart-gesture-hint"', html)
        self.assertIn('CTRL/⌘ OR ALT + WHEEL TO ZOOM', html)
        self.assertIn('function isChartWheelZoomGesture(event)', html)
        self.assertIn('return Boolean(event.ctrlKey || event.metaKey || event.altKey);', html)
        self.assertIn('if (!isChartWheelZoomGesture(event)) return;\n    event.preventDefault();', html)

    def test_data_management_centralizes_upload_review_match_build_and_library(self):
        html = render_dashboard_html()
        data_panel = html.split('section id="tab-data-management"', 1)[1].split(
            'section id="tab-audit"', 1
        )[0]
        self.assertIn('data-data-subtab="upload"', data_panel)
        self.assertIn('data-data-subtab="review"', data_panel)
        self.assertIn('data-data-subtab="match"', data_panel)
        self.assertIn('data-data-subtab="library"', data_panel)
        self.assertIn('section id="data-subtab-upload"', data_panel)
        self.assertIn('section id="data-subtab-review"', data_panel)
        self.assertIn('section id="data-subtab-match"', data_panel)
        self.assertIn('section id="data-subtab-library"', data_panel)
        self.assertIn('id="upload-prospectus"', data_panel)
        self.assertIn('id="upload-market-data"', data_panel)
        self.assertIn('id="save-contract-terms"', data_panel)
        self.assertIn('id="approve-contract-terms"', data_panel)
        self.assertIn('id="match-uploaded-market-data"', data_panel)
        self.assertIn('id="generate-valuation-history"', data_panel)
        self.assertIn('id="source-library-details"', data_panel)
        self.assertIn('id="sources-table"', data_panel)
        self.assertNotIn('id="tab-prospectus-intake"', html)
        self.assertNotIn('id="tab-data-intake"', html)
        self.assertNotIn('id="tab-data-sources"', html)
        self.assertEqual(html.count('data-upload-kind="prospectus"'), 1)
        self.assertEqual(html.count('data-upload-kind="market_data_auto"'), 1)
        self.assertIn('data-upload-input="upload-prospectus" data-upload-status="prospectus-upload-status"', html)
        self.assertIn('data-upload-kind="market_data_auto" data-upload-input="upload-market-data"', html)
        self.assertIn('accept="application/pdf,.pdf" multiple aria-label="Choose prospectus PDF files"', html)
        self.assertIn('accept=".csv,.xlsx" multiple aria-label="Choose CSV or Excel market data files"', html)
        self.assertIn('Terms are extracted automatically. You still approve them before pricing.', html)
        self.assertIn('Add bond documents and dated market prices in one place.', html)
        self.assertIn('id="match-uploaded-market-data" class="cmd-primary">Match uploaded prices</button>', html)
        self.assertIn('id="market-match-status" class="small muted" role="status"', html)
        self.assertIn('async function matchUploadedMarketPrices(options={})', html)
        self.assertIn("document.getElementById('match-uploaded-market-data').addEventListener('click', matchUploadedMarketPrices);", html)
        self.assertIn("if (kind !== 'market_data_auto') body.contract_id = selected.id || '';", html)
        self.assertIn('id="market-data-guide" class="market-guide"', html)
        self.assertIn('<h3>Build valuation history</h3>', html)
        self.assertIn('id="generate-valuation-history" class="cmd-primary" data-market-action="build" disabled', html)
        self.assertIn('id="upload-market-data-button"', html)
        self.assertNotIn('uploadInput.disabled = true', html)
        self.assertNotIn('uploadButton.disabled = true', html)
        self.assertGreaterEqual(html.count('uploadInput.disabled = false'), 3)
        self.assertGreaterEqual(html.count('uploadButton.disabled = false'), 3)
        self.assertIn('You can upload prices now. Add the final ISIN under Review and approve before matching them.', html)
        self.assertIn('Upload more prices', html)
        self.assertIn('Upload prices for overlapping dates', html)
        self.assertIn('Existing uploaded prices will be kept.', html)
        self.assertNotIn('Upload replacement prices', html)
        self.assertNotIn('Upload replacement files', html)
        self.assertNotIn('Upload replacement dates', html)
        self.assertNotIn('Replace market files', html)
        self.assertIn("return lines.slice(0, 3).join('\\n');", html)
        self.assertNotIn('data-upload-input="upload-raw-price"', html)
        self.assertNotIn('data-upload-kind="market_data_history"', html)
        self.assertNotIn('data-upload-kind="market_history_csv"', html)
        self.assertNotIn('data-upload-input="upload-prospectus-inbox"', html)
        self.assertNotIn('id="inbox-upload-status"', html)
        self.assertIn('id="extract-selected-prospectuses"', html)
        self.assertIn('disabled>Extract selected (0)</button>', html)
        self.assertIn('<input type="checkbox" name="raw-pdf-row"', html)
        self.assertNotIn('<input type="radio" name="raw-pdf-row"', html)
        self.assertIn("const selectedCount = selectedSourcePaths().length;", html)
        self.assertIn("selectedButton.disabled = extractionRunning || selectedCount === 0;", html)
        self.assertIn("filter(item => item && (reviewBucket(item) === 'pending_extraction' || !item.contract_path))", html)
        self.assertIn("preferredSourcePaths:uploadedProspectusPaths", html)
        self.assertIn("renderReviewSelection();", html)
        self.assertIn("function toggleReviewSelection(index, event={})", html)
        self.assertIn("event.shiftKey", html)
        self.assertIn("if (disclosure) disclosure.open = true;", html)
        self.assertIn("await extractPendingProspectuses(null, 'selected');", html)
        self.assertIn('id="pdf-extraction-queue" class="technical-details"', html)
        self.assertIn("if (extractionNeedsAttention) document.getElementById('pdf-extraction-queue').open = true;", html)
        self.assertNotIn("Next: select the uploaded PDF below", html)

    def test_data_management_navigation_has_no_stale_split_targets(self):
        html = render_dashboard_html()

        for token in (
            "getElementById('open-active-terms')",
            "getElementById('view-summary-terms')",
            "getElementById('open-active-market-data')",
            "activateTab('prospectus-intake')",
            "activateTab('data-intake')",
            "activateTab('data-sources')",
            "activateProspectusSubtab(",
        ):
            self.assertNotIn(token, html)

        self.assertIn("function activateDataSubtab(requestedName)", html)
        self.assertIn(
            "document.querySelectorAll('.subtab-button[data-data-subtab]')",
            html,
        )
        self.assertIn(
            "document.querySelectorAll('#tab-data-management .subtab-panel')",
            html,
        )
        self.assertNotIn(
            "document.querySelectorAll('.subtab-panel').forEach",
            html,
        )
        self.assertEqual(html.count('id="prospectus-status-strip"'), 1)

    def test_output_and_assumptions_stay_free_of_data_workflow_controls(self):
        html = render_dashboard_html()
        summary_panel = html.split('section id="tab-pm-view"', 1)[1].split(
            'section id="tab-valuation"', 1
        )[0]
        assumptions_panel = html.split('section id="tab-assumptions"', 1)[1].split(
            'section id="tab-priced-rows"', 1
        )[0]

        for control_id in (
            'upload-prospectus',
            'upload-market-data',
            'save-contract-terms',
            'approve-contract-terms',
            'match-uploaded-market-data',
            'generate-valuation-history',
            'sources-table',
        ):
            self.assertNotIn(f'id="{control_id}"', summary_panel)
            self.assertNotIn(f'id="{control_id}"', assumptions_panel)

        self.assertIn('id="view-summary-data"', summary_panel)
        self.assertIn('<input name="contract_path" type="hidden">', assumptions_panel)
        self.assertNotIn('Contract JSON <input', assumptions_panel)

    def test_pending_raw_delete_can_resolve_filename_only_queue_rows(self):
        html = render_dashboard_html()
        self.assertIn("function reviewItemSourcePath(item)", html)
        self.assertIn("item?.source_filename ? `data/raw/prospectuses/${item.source_filename}`", html)
        self.assertIn("function selectedRawSourcePath()", html)
        self.assertIn("return reviewItemSourcePath(selectedReviewItem);", html)
        self.assertIn("function selectedPendingRawProspectuses()", html)
        self.assertIn(".map(index => reviewItems[index])", html)

    def test_file_deletion_uses_confirm_only_and_sequences_bulk_selection(self):
        html = render_dashboard_html()

        for typed_prompt in (
            "Type the filename to remove",
            "Type the raw prospectus filename to confirm deletion",
            "Type the contract id or raw prospectus filename to confirm deletion",
            "Deletion requires typed confirmation",
        ):
            self.assertNotIn(typed_prompt, html)

        helper = html.split("async function confirmFileDeletions(items, deleteFile)", 1)[1].split(
            "function cbDisplayLabel", 1
        )[0]
        self.assertIn("for (let index = 0; index < items.length; index += 1)", helper)
        self.assertIn("if (!confirm(`Delete", helper)
        self.assertIn("${index + 1} of ${items.length}", helper)
        self.assertIn("result.skipped += 1;\n      continue;", helper)
        self.assertIn("await deleteFile(item);", helper)

        remove_flow = html.split("async function removeSelectedSource()", 1)[1].split(
            "async function editSelectedSource()", 1
        )[0]
        self.assertIn("const items = selectedSourcesForRemoval();", remove_flow)
        self.assertIn("confirmFileDeletions(removable", remove_flow)
        self.assertIn("typed_confirmation:item.filename", remove_flow)
        self.assertIn("{reload:false}", remove_flow)

        pending_flow = html.split("async function deleteSelectedPendingRawProspectus()", 1)[1].split(
            "function updateProspectusActionState()", 1
        )[0]
        self.assertIn("const items = selectedPendingRawProspectuses();", pending_flow)
        self.assertIn("confirmFileDeletions(items", pending_flow)
        self.assertIn("typed_confirmation:item.filename", pending_flow)

        linked_flow = html.split("async function deleteRawProspectus()", 1)[1].split(
            "function renderCharts(payload)", 1
        )[0]
        self.assertIn("if (!confirm(`Delete", linked_flow)
        self.assertIn("typed_confirmation:confirmation", linked_flow)
        self.assertNotIn("prompt(", linked_flow)
        self.assertIn(
            "Deletion asks for confirmation and still runs all backend safety checks.",
            html,
        )

    def test_progress_ui_is_reusable_for_price_preview_and_data_workflows(self):
        html = render_dashboard_html()
        self.assertIn('id="extraction-progress" class="progress-wrap"', html)
        self.assertIn('id="source-link-progress" class="progress-wrap"', html)
        self.assertIn('id="source-link-progress-text"', html)
        self.assertIn('id="market-build-progress" class="progress-wrap"', html)
        self.assertIn('id="market-build-progress-text"', html)
        self.assertIn('id="price-preview-progress" class="progress-wrap"', html)
        self.assertIn('id="price-preview-progress-text"', html)
        self.assertIn("function setProgressBar(config, active, text='', percent=0)", html)
        self.assertIn("setProgressBar(PROGRESS_COMPONENTS.extraction", html)
        self.assertIn("setProgressBar(PROGRESS_COMPONENTS.sourceLink", html)
        self.assertIn("setProgressBar(PROGRESS_COMPONENTS.marketBuild", html)
        self.assertIn("setProgressBar(PROGRESS_COMPONENTS.pricePreview", html)
        self.assertIn("setSourceActionControls(true)", html)
        self.assertIn("setSourceActionControls(false)", html)
        self.assertIn("setPricePreviewControls(true)", html)
        self.assertIn("setPricePreviewProgress(true, 'Pricing the base valuation rows.', 10)", html)
        self.assertIn("const sensitivityResult = await renderPayload(payload, generation, body);", html)
        self.assertIn("return runSensitivityGrid(payload, generation, pricingGeneration, baseRequestBody);", html)
        self.assertIn("const baseBody = {...baseRequestBody};", html)
        sensitivity_flow = html.split("async function runSensitivityGrid", 1)[1].split(
            "function renderAudit", 1
        )[0]
        self.assertEqual(sensitivity_flow.count("fetch("), 1)
        self.assertIn("fetch('/api/price-preview-sensitivity'", sensitivity_flow)
        self.assertNotIn("fetch('/api/price-preview',", sensitivity_flow)
        self.assertEqual(html.count("fetch('/api/price-preview',"), 1)
        self.assertGreaterEqual(
            sensitivity_flow.count(
                "generation !== sensitivityGeneration || pricingGeneration !== pricingLoadGeneration"
            ),
            3,
        )
        self.assertIn("Calculating 9 sensitivity scenarios in one batch.", html)
        self.assertIn("return {complete:true, failureCount, scenarioCount};", html)
        self.assertIn("pricePreviewRunning = running;", html)
        self.assertIn("button.disabled = pricePreviewRunning || !readiness.ready;", html)
        self.assertIn("form.querySelectorAll('input:not([type=\"hidden\"]), select')", html)
        self.assertIn("function invalidatePricePreview()", html)
        self.assertIn("Sensitivity scenarios calculated (", html)
        self.assertIn("setPricePreviewProgress(true, completion, 100, 'complete');", html)
        self.assertIn("setPricePreviewProgress(true, completion, 100, 'warning');", html)
        self.assertIn("setPricePreviewProgress(true, 'Price preview failed:", html)
        upload_flow = html.split("async function uploadSelectedFile", 1)[1].split(
            "function uploadResultText", 1
        )[0]
        self.assertNotIn(
            "await loadUniverse({preferredContractPaths:[selected.contract_path].filter(Boolean), price:shouldPriceAfterUploads});\n    applySelectedCb();",
            upload_flow,
        )
        self.assertNotIn("applySelectedCb();", upload_flow)
        self.assertLess(html.index('id="generate-valuation-history"'), html.index('id="source-link-progress"'))
        self.assertLess(html.index('Price Preview</button>'), html.index('id="price-preview-progress"'))

    def test_terms_review_buttons_use_compact_command_group_style(self):
        html = render_dashboard_html()
        terms_idx = html.index('data-data-subtab="review"')
        group_idx = html.index('class="intake-action-row command-group terms-action-group" aria-label="Terms review actions"')
        save_idx = html.index('id="save-contract-terms" aria-label="Save changed terms" title="Save changed terms">Save changes</button>')
        approve_idx = html.index('id="approve-contract-terms" class="cmd-primary" aria-label="Approve terms and continue" title="Approve terms and continue">Approve &amp; continue</button>')
        reload_idx = html.index('id="load-contract-review" class="cmd-utility" aria-label="Reload terms for selected CB" title="Reload terms for selected CB">Reload</button>')
        self.assertLess(terms_idx, group_idx)
        self.assertLess(group_idx, save_idx)
        self.assertLess(save_idx, approve_idx)
        self.assertLess(approve_idx, reload_idx)
        self.assertIn('<span class="command-label">TERMS</span>', html)
        self.assertNotIn('>Approve terms for pricing</button>', html)
        self.assertNotIn('>Save edits; keep in review</button>', html)
        self.assertNotIn('>Reload terms for selected CB</button>', html)
        self.assertIn('data-tab="data-management">Data</button>', html)
        self.assertIn('id="open-data-management"', html)
        self.assertIn('id="view-summary-data"', html)
        self.assertIn("async function openActiveTerms()", html)
        self.assertIn("const selected = selectedUniverseItem();\n  if (selected?.contract_path)", html)
        open_terms = html.split("async function openActiveTerms()", 1)[1].split("function fmtBytes", 1)[0]
        self.assertIn("selectedReviewItem = null;", open_terms)
        self.assertIn("renderSelectedPendingReview();", open_terms)
        self.assertIn("if (extracted.length === 1)", open_terms)
        self.assertLess(open_terms.index("selectedReviewItem = null;"), open_terms.index("activateDataSubtab('upload')"))
        self.assertIn("activateDataSubtab('upload');\n    focusDestination('data-management', 'upload');\n    return;", html)
        self.assertIn("activateDataSubtab('review');", html)
        self.assertIn("if (selectedReviewItem?.contract_path) await loadSelectedContractReview();", html)
        self.assertIn("document.querySelectorAll('.subtab-button[data-data-subtab]')", html)
        self.assertIn("await syncSelectedContractReviewFromDropdown();\n  activateTab('data-management');", html)
        self.assertIn("syncActiveUniverseContract(selectedReviewItem.contract_path);\n  activateTab('data-management');", html)

    def test_terms_review_surfaces_economics_attention_and_a_clear_next_step(self):
        html = render_dashboard_html()

        self.assertEqual(CONTRACT_FIELD_LABELS["instrument.canonical_id_type"], "Identifier status")
        self.assertEqual(
            CONTRACT_FIELD_CHOICES["instrument.canonical_id_type"],
            [
                {"value": "ISIN", "label": "ISIN assigned"},
                {"value": "PENDING_ISIN", "label": "ISIN pending"},
            ],
        )
        self.assertEqual(CONTRACT_FIELD_LABELS["bond.brokerage"], "Brokerage (%)")
        self.assertEqual(CONTRACT_FIELD_LABELS["bond.investor_offer_price"], "Investor offer price (per 100)")
        self.assertEqual(CONTRACT_FIELD_LABELS["redemption.yield_to_maturity"], "Quoted YTM (%)")
        self.assertEqual(CONTRACT_FIELD_LABELS["puts.0.yield_to_put"], "Yield to first put (%)")
        self.assertIn('id="terms-next-step" class="next-step-card"', html)
        self.assertIn('function updateDerivedInvestorOffer()', html)
        self.assertIn('issueValue + brokerageValue', html)
        self.assertIn("if (input.readOnly || input.disabled) return;", html)
        self.assertIn('<details class="term-evidence">', html)
        self.assertIn('<details class="term-group">', html)
        self.assertIn('Needs attention', html)
        self.assertIn('Key terms', html)
        self.assertIn('function renderTermsNextStep(payload)', html)
        self.assertIn("async function loadMarketGenerationReadiness(contractPathOverride='')", html)
        self.assertIn("const readiness = await loadMarketGenerationReadiness(contractPath);", html)
        self.assertIn("Object.keys(changedContractEdits()).length", html)
        self.assertIn("Identifier status will switch to ISIN assigned.", html)
        self.assertIn("Set Identifier status to ISIN assigned before matching market prices.", html)
        self.assertIn("function focusContractField(fieldName)", html)
        self.assertIn("const disclosure = input?.closest('details');", html)
        self.assertIn("if (disclosure) disclosure.open = true;", html)
        self.assertIn("let reviewQueueLoadGeneration = 0;", html)
        self.assertIn("if (generation !== reviewQueueLoadGeneration) return false;", html)
        self.assertIn("let pricingLoadGeneration = 0;", html)
        self.assertIn("generation !== pricingLoadGeneration || selectedUniverseItem()?.contract_path !== contractPath", html)
        self.assertIn("function updateIdentifierStatusFromId()", html)
        self.assertIn("if (event.currentTarget.dataset.contractField === 'instrument.canonical_id')", html)
        self.assertIn("syncActiveUniverseContract(contractPath);", html)
        self.assertIn("if (latestPayload && !sourcePathsMatch(latestPayload?.inputs?.contract_path, path))", html)
        self.assertIn("if (tab === 'pm-view') await loadPricing();", html)
        self.assertIn("let contractReviewLoadGeneration = 0;", html)
        self.assertIn("generation !== contractReviewLoadGeneration || intendedReviewContractPath() !== path", html)
        self.assertIn("approval blocker(s) must be resolved.", html)
        self.assertIn("upload(s) also failed.", html)
        self.assertIn("Array.isArray(payload.processed_items)", html)
        self.assertIn('Build valuation history', html)
        self.assertIn('Upload market prices', html)
        self.assertIn('View summary', html)
        self.assertIn("await loadUniverse({preferredContractPaths:[payload.contract_path], price:false});", html)
        self.assertNotIn('Evidence snippets</h2>', html)

    def test_assumption_action_buttons_use_compact_command_group_style(self):
        html = render_dashboard_html()
        group_idx = html.index('class="button-row intake-action-row command-group assumption-action-group" aria-label="Pricing assumption actions"')
        preview_idx = html.index('type="submit" class="cmd-primary" aria-label="Run price preview" title="Run non-persistent price preview">Price Preview</button>')
        save_idx = html.index('id="save-assumptions" aria-label="Save assumption set" title="Save current assumptions as an append-only set">Save Assumption Set</button>')
        self.assertLess(group_idx, preview_idx)
        self.assertLess(preview_idx, save_idx)
        assumption_group = html[group_idx:html.index('</div>', group_idx)]
        self.assertNotIn('refresh-selected-cb', assumption_group)
        self.assertGreater(
            html.index('id="refresh-selected-cb"'),
            html.index('id="data-subtab-match"'),
        )
        self.assertIn('<span class="command-label">PRICE</span>', html)
        self.assertIn('.intake-action-row.command-group', html)
        self.assertIn('.intake-action-row button { padding:4px 7px; min-height:24px; font-size:var(--text-xs);', html)

    def test_layout_uses_adaptive_type_grids_and_narrow_screen_containment(self):
        html = render_dashboard_html()

        for token in ('--text-xs', '--text-sm', '--text-base', '--text-md', '--text-lg', '--text-xl', '--text-command'):
            self.assertIn(f'{token}:clamp(', html)
        self.assertIn('grid-template-columns:repeat(auto-fit,minmax(min(145px,100%),1fr))', html)
        self.assertIn('grid-template-columns:repeat(auto-fit,minmax(min(135px,100%),1fr))', html)
        self.assertIn('grid-template-columns:repeat(auto-fit,minmax(min(250px,100%),1fr))', html)
        self.assertIn('.terminal-task-grid { display:grid; grid-template-columns:minmax(0,1.25fr) minmax(260px,.75fr);', html)
        self.assertIn('.tab-bar { display:flex; gap:2px; flex-wrap:wrap;', html)
        self.assertIn('.secondary-tab-bar { position:absolute;', html)
        self.assertIn('overflow-x:auto; overscroll-behavior-inline:contain; scrollbar-width:thin;', html)
        self.assertIn('@media (max-width:640px)', html)
        self.assertIn('@media (max-width:420px)', html)
        self.assertIn('.tab-bar { display:grid; grid-template-columns:repeat(2,minmax(0,1fr));', html)
        self.assertIn('.terminal-task-grid { grid-template-columns:1fr; }', html)
        self.assertIn('main input:not([type="checkbox"]):not([type="radio"]), main select, main textarea { font-size:16px; }', html)
        self.assertIn('.intake-toolbar { position:static; }', html)
        self.assertIn('.term-table .term-row { display:block;', html)
        self.assertIn('.term-table td:nth-child(3)::before { content:"Source"; }', html)
        self.assertIn('pre { max-width:100%; white-space:pre-wrap; overflow-wrap:anywhere;', html)
        self.assertNotIn('.overview-hero', html)
        self.assertNotIn('.journey-grid', html)
        self.assertNotIn('.terms-callout', html)

    def test_assumptions_form_is_clean_and_keeps_data_paths_hidden(self):
        html = render_dashboard_html()

        self.assertIn('name="volatility" type="number" step="0.01"></label>', html)
        self.assertIn('name="risk_free_rate" type="hidden">', html)
        self.assertIn('name="credit_spread" type="number" step="1"></label>', html)
        self.assertIn('name="borrow_rate" type="number" step="0.01"></label>', html)
        self.assertIn('name="dividend_yield" type="number" step="0.01"></label>', html)
        self.assertIn('name="steps" type="number" min="3" max="500" value="250"></label>', html)
        self.assertNotIn('<option value="">Select model...</option>', html)
        self.assertIn('name="scenario_name" value="base"></label>', html)
        self.assertIn('name="risk_free_source" id="risk-free-source"', html)
        for currency in ("USD", "HKD", "TWD", "CNY", "JPY", "KRW", "AUD"):
            self.assertIn(f'<option value="{currency}">{currency} yield curve</option>', html)
        self.assertIn('<option value="manual" selected>Manual</option>', html)
        self.assertIn('Manual risk-free rate (%)', html)
        self.assertIn('id="manual-risk-free-field"', html)
        self.assertIn('name="manual_rf_display" type="number" step="0.01" oninput=', html)
        self.assertIn('<input name="contract_path" type="hidden">', html)
        self.assertIn('<input name="market_history_path" type="hidden">', html)
        self.assertIn('<input name="raw_price_history_path" type="hidden">', html)
        self.assertNotIn('<summary>Advanced file inputs</summary>', html)
        self.assertNotIn('Contract JSON <input', html)
        self.assertNotIn('Market history CSV <input', html)
        self.assertNotIn('Raw quote history XLSX/CSV <input', html)
        self.assertIn('<summary>Advanced assumptions</summary>', html)
        self.assertNotIn('name="use_yield_curve" type="checkbox"', html)
        self.assertIn('function selectedRiskFreeSource()', html)
        self.assertIn('function applyRiskFreeSourceToPayload(body)', html)
        self.assertIn("body.use_yield_curve = source !== 'manual';", html)
        self.assertIn("body.yield_curve_currency = source === 'manual' ? '' : source;", html)
        self.assertNotIn('value="38"', html)
        self.assertNotIn('value="160"', html)
        self.assertIn('value="250"', html)
        self.assertIn('value="base"', html)
        self.assertIn('<option value="tf_split_tree" selected>', html)
        self.assertIn('id="assumption-gate" class="assumption-gate"', html)
        self.assertIn('id="assumption-readiness" class="assumption-readiness"', html)
        self.assertIn('function assumptionReadiness()', html)
        self.assertIn('Pricing assumptions required:', html)

    def test_yield_curve_override_is_visible_in_dashboard_copy_and_outputs(self):
        html = render_dashboard_html()

        self.assertIn("The bond's economic principal currency is", html)
        self.assertIn('not the stock trading currency', html)
        self.assertIn('Online curves replay the current curve across historical rows', html)
        self.assertIn('Curve target', html)
        self.assertIn('Risk-free source', html)
        self.assertIn('yield-curve-chart', html)
        self.assertIn('Matched maturity', html)

    def test_yield_curve_chart_uses_tenor_scaled_x_axis_and_clamped_match_marker(self):
        html = render_dashboard_html()

        self.assertIn("xValueKey:'years'", html)
        self.assertIn("xLabel:'Tenor (years)'", html)
        self.assertIn("function clampPlotX(value, xMin, xMax)", html)
        self.assertIn("const markerYears = clampPlotX(Number(latest.target_years), minX, maxX);", html)
        self.assertIn("const x = value =>", html)
        self.assertIn("shown at available curve edge", html)
        self.assertNotIn("const x = v => P.left + (Number(v)-minX)/xSpan*(W-P.left-P.right);", html)

    def test_dashboard_html_contains_cb_terminal_branding_without_legacy_names(self):
        html = render_dashboard_html()
        self.assertIn("<h1>CB TERMINAL</h1>", html)
        self.assertIn('<div class="terminal-badge">LOCAL</div>', html)
        self.assertNotIn("From prospectus to reviewed valuation.", html)
        self.assertNotIn("Runs locally · Files stay here", html)
        self.assertNotIn("Bloomberg-style local terminal", html)
        forbidden = ["CB " + "Arb", "CB " + "Arbi" + "trage", "cb" + "-arb", "cb" + "_arb", "Cb" + "Arb"]
        for old_name in forbidden:
            self.assertNotIn(old_name, html)

    def test_data_library_preserves_source_actions_inside_data_management(self):
        html = render_dashboard_html()
        self.assertIn('id="data-subtab-library"', html)
        self.assertIn('Uploaded files and troubleshooting', html)
        self.assertIn('Normal upload, approval, and matching stay in the first three steps.', html)
        self.assertNotIn('Use Market data for the normal upload-and-build workflow.', html)
        self.assertIn('id="source-library-details" class="source-library"', html)
        self.assertIn('id="refresh-source-matches"', html)
        self.assertIn('id="generate-valuation-history"', html)
        self.assertIn('Build valuation history', html)
        self.assertIn('Used by / coverage', html)
        self.assertIn('Identifiers found', html)
        self.assertIn("async function generateValuationHistory()", html)
        self.assertIn("function activeContractPath()", html)
        self.assertIn("if (raw === ''", html)
        self.assertIn("contract_path:contractPath", html)
        self.assertIn("payload.status === 'no_overlap'", html)
        self.assertIn("fetch('/api/market-generation-readiness'", html)
        self.assertIn('id="market-generation-readiness"', html)
        self.assertIn("renderMarketGenerationReadiness", html)
        self.assertIn("function renderMarketDataGuide(payload)", html)
        self.assertIn("await matchUploadedMarketPrices({contractPath:createdPaths[0]});", html)
        self.assertIn("await matchUploadedMarketPrices({contractPath});", html)
        self.assertIn("Existing uploaded prices were checked automatically.", html)
        self.assertIn("No exact match found. Still needed:", html)
        self.assertIn("function marketSourceMatchGroups(item)", html)
        self.assertIn("return 'For another security';", html)
        self.assertIn("let marketReadinessLoadGeneration = 0;", html)
        self.assertIn("generation !== marketReadinessLoadGeneration || activeContractPath() !== contractPath", html)
        self.assertIn("status: 'loading'", html)
        self.assertIn("payload.status === 'needs_terms_approval'", html)
        self.assertIn("latestMarketReadiness = payload.readiness;", html)
        self.assertIn("buildButton.disabled = sourceActionRunning || payload.status !== 'ready';", html)
        self.assertIn("payload.linked_history?.can_update_from_sources", html)
        self.assertIn("historyNeedsUpdate ? 'Update valuation history' : 'Build valuation history'", html)
        self.assertIn("Update the valuation history to add new dates and refresh matching dates.", html)
        self.assertNotIn("preserve_linked_history", html)
        self.assertIn("selectedSourceIds = new Set(Array.from(selectedSourceIds).filter", html)
        self.assertNotIn("selectedSourceIds.add(selectedSourceId)", html)
        self.assertNotIn("Click row for details; use the checkbox to select.", html)
        self.assertNotIn('Generate valuation CSV', html)
        self.assertIn("fetch('/api/generate-valuation-market-history'", html)
        self.assertNotIn('id="link-source"', html)
        self.assertNotIn('Link to selected CB', html)
        self.assertNotIn("payload.sync_status === 'linked_to_selected_cb'", html)

    def test_table_row_clicks_select_sources_and_refresh_extracted_bond_terms(self):
        html = render_dashboard_html()

        source_selection = html.split(
            "function selectSourceRow(sourceId, visibleRows, event={})", 1
        )[1].split("function renderSourceDetail()", 1)[0]
        self.assertIn(
            "} else {\n    if (selectedSourceIds.has(sourceId))",
            source_selection,
        )
        self.assertNotIn("else if (fromCheckbox)", source_selection)
        self.assertIn(
            "if (event.stopPropagation && fromCheckbox) event.stopPropagation();",
            source_selection,
        )

        review_selection = html.split("function renderReviewSelection()", 1)[1].split(
            "function selectReviewSelection(index)", 1
        )[0]
        self.assertIn(
            'input[type="checkbox"], input[type="radio"]',
            review_selection,
        )
        self.assertIn("control.type === 'radio'", review_selection)
        self.assertIn(
            "? selectedReviewItem === reviewItems[idx]",
            review_selection,
        )

        contract_loading = html.split(
            "async function loadSelectedContractReview()", 1
        )[1].split("function changedContractEdits()", 1)[0]
        self.assertIn(
            "status.textContent = `Loading key terms for ${selectedLabel}...`;",
            contract_loading,
        )
        self.assertIn(
            "generation !== contractReviewLoadGeneration || intendedReviewContractPath() !== path",
            contract_loading,
        )

    def test_cb_refresh_and_data_source_linking_reprice_selected_contract(self):
        html = render_dashboard_html()

        self.assertIn('id="refresh-selected-cb"', html)
        self.assertIn('Refresh selected bond', html)
        self.assertIn("document.getElementById('refresh-selected-cb').addEventListener('click', refreshSelectedCb);", html)
        self.assertIn("async function refreshSelectedCb()", html)
        self.assertIn("async function refreshActiveInstrumentTabs(options={})", html)
        self.assertIn("await refreshActiveInstrumentTabs({source:'command'});", html)
        self.assertIn("cbSelect.addEventListener('change', () => { applySelectedCb(); refreshActiveInstrumentTabs(); });", html)
        self.assertIn("await loadPricing();\n  await loadSources();\n  if (selectedUniverseItem()?.contract_path !== contractPath) return;\n  const reviewQueueLoaded = await loadReviewQueue({\n    preferredContractPaths:[contractPath],\n    requiredActiveContractPath:contractPath\n  });\n  if (!reviewQueueLoaded || selectedUniverseItem()?.contract_path !== contractPath) return;\n  await syncSelectedContractReviewFromDropdown();\n  await loadMarketGenerationReadiness();", html)
        self.assertIn("Loaded ${label}. Summary, Data, and Assumptions are ready to inspect.", html)
        self.assertIn("await loadUniverse({preferredContractPaths:[selected.contract_path], price:false});", html)
        self.assertIn("clearPricingView(", html)
        self.assertIn("latestPayload = null;", html)
        self.assertIn("function pricingReadinessMessage(selected)", html)
        self.assertIn("const readiness = selected?.readiness ||", html)
        self.assertIn("missing.includes('valuation_market_history')", html)
        self.assertIn("raw quotes linked; generate market history", html)
        self.assertIn("SQLite source", html)
        self.assertIn("Canonical source id", html)
        self.assertIn("function cbDisplayLabel(item)", html)
        self.assertIn("return item.display_id || item.instrument_display_name", html)
        self.assertIn("esc(cbDisplayLabel(item))", html)
        self.assertIn("const label = cbDisplayLabel(selected);", html)
        self.assertIn("async function generateValuationHistory()", html)
        self.assertIn("function activeContractPath()", html)
        self.assertIn("if (raw === ''", html)
        self.assertIn("contract_path:contractPath", html)
        self.assertIn("payload.status === 'no_overlap'", html)
        self.assertIn("fetch('/api/market-generation-readiness'", html)
        self.assertIn('id="market-generation-readiness"', html)
        self.assertIn("renderMarketGenerationReadiness", html)
        self.assertIn("fetch('/api/generate-valuation-market-history'", html)
        self.assertIn("payload.source_link", html)
        self.assertNotIn("payload.sync_status === 'linked_to_selected_cb'", html)
        self.assertGreater(html.index('id="refresh-selected-cb"'), html.index('id="save-assumptions"'))
        self.assertIn("function selectCbFromCommand(raw)", html)
        self.assertIn("function cbSearchText(item)", html)
        self.assertIn("function renderCbCommandSuggestions(query='')", html)
        self.assertIn("function commandCompletionCandidate(query)", html)
        self.assertIn("function acceptCommandGhostCompletion()", html)
        self.assertIn("event.key === 'Tab' && acceptCommandGhostCompletion()", html)
        self.assertIn("cbCommandInput.setAttribute('aria-activedescendant'", html)
        self.assertIn('.instrument-nav { position:relative; border-bottom:1px solid #565656', html)
        self.assertIn("function handleCbCommandKeydown(event)", html)
        self.assertIn("function selectCbByIndex(idx)", html)
        self.assertIn("if (!selected) { clearPricingView('Search for a bond above to load its valuation.'); statusEl.textContent = 'No bond selected.'; return; }", html)
        self.assertIn("statusEl.textContent = universeItems.length ? 'Bond library loaded. Search by ISIN, issuer, ticker, or display name.' : 'No covered bonds loaded.';", html)
        self.assertNotIn("selectedIndex = universeItems.findIndex(item => item.available_for_pricing);", html)


if __name__ == "__main__":
    unittest.main()
