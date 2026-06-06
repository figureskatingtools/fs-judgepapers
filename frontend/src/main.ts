import './style.css'
import { renderSiteNav, initSiteNav, injectSiteNavStyles } from '@figureskatingtools/shared-ui';
import { validateCategory, validateCompetition } from './validate';

// Inject the shared figureskatingtools.com nav styles once at startup
injectSiteNavStyles();

/** Escape HTML special characters to prevent XSS */
function escapeHtml(str: string): string {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

interface ClientPrincipal {
  userId: string;
  userRoles: string[];
  identityProvider: string;
  userDetails: string;
}

interface CategoryInfo {
  abbreviation: string;
  displayName: string;
  displayNameFi: string;
  judgingMethod: string;
  competitionType: string;
}

// Module-level categories cache, loaded once from the API
let categoriesCache: CategoryInfo[] = [];

// Language setting: 'fi' (Finnish, default) or 'en' (English)
let currentLanguage: 'fi' | 'en' = 'fi';

async function loadCategoriesCache() {
    try {
        const resp = await fetch('/api/get_categories');
        if (resp.ok) {
            categoriesCache = await resp.json();
        }
    } catch (_e) {
        console.warn('Failed to load categories');
    }
}

/**
 * Get the localized category name from file data in the structure.
 * The structure keys are English displayName values. Files within contain
 * categoryFi for Finnish names.
 */
function getLocalizedCategoryName(categoryKey: string, segments: Record<string, any[]>): string {
    if (currentLanguage === 'fi') {
        // Try to find categoryFi from any file in this category
        for (const segFiles of Object.values(segments)) {
            for (const file of segFiles) {
                if (file.categoryFi) return file.categoryFi;
            }
        }
    }
    return categoryKey;
}

function isMupiCategory(categoryCode: string): boolean {
    const cat = categoriesCache.find(c => c.abbreviation === categoryCode);
    return cat?.judgingMethod === 'MUPI';
}

const appElement = document.querySelector<HTMLDivElement>('#app')!;

// Initial basic layout structure. The site nav (shared across all
// figureskatingtools.com apps) is rendered into #site-nav-container by init()
// once the auth state is known.
appElement.innerHTML = `
  <div id="site-nav-container"></div>

  <main>
    <div id="loading-view" class="loading-screen">
      <h2>Authenticating...</h2>
      <p>Please wait while we verify your credentials.</p>
    </div>

    <div id="error-view" class="error-screen hidden">
        <!-- Error content injected dynamically -->
    </div>

    <div id="landing-view" class="hidden">
      <div class="card landing-card reveal">
        <span class="micro-label">Judge Paper Creator</span>
        <h2>Create Judging Papers with Ease</h2>
        <p class="lead">
          This application provides an easy way to create judging papers for figure skating competitions.
          Simply upload the PDF exports from <strong>Figure Skating Manager</strong>, and we handle the rest.
        </p>
        <div class="landing-contact">
            <p>
                To access the application, please contact the administrator:
            </p>
             <a href="mailto:markus@lintuala.fi">markus@lintuala.fi</a>
        </div>
        <div style="margin-top: 2rem;">
            <a href="/.auth/login/aad?post_login_redirect_url=/" class="btn btn-primary">Sign In to Continue</a>
        </div>
      </div>
    </div>

    <div id="main-content" class="hidden">
      <!-- Modal Container -->
      <div id="modal-overlay" class="modal-overlay hidden">
        <div class="modal">
            <h3 id="modal-title">Confirm Action</h3>
            <p id="modal-message" class="modal-message">Are you sure?</p>

            <div id="modal-extra-content" style="margin-bottom: 1.5rem;">
                <!-- Dynamic Content like Checkbox -->
            </div>

            <div class="modal-actions">
                <button id="modal-cancel" class="btn btn-ghost btn-sm">Cancel</button>
                <button id="modal-confirm" class="btn btn-primary btn-sm">Confirm</button>
            </div>
        </div>
      </div>

      <div id="view-competitions" class="hidden">
        <div class="card reveal">
            <div class="view-header">
                <h2>Competitions</h2>
                <button id="btn-create-comp" class="btn btn-primary btn-sm">Create New</button>
            </div>
            <div id="competitions-list">
                <p class="text-muted">Loading competitions...</p>
            </div>
        </div>
      </div>

      <div id="view-competition-details" class="hidden">
        <div class="card reveal">
             <div class="view-header">
                <div class="view-header-lead">
                    <button id="btn-back-list" class="btn btn-sm btn-ghost">← Back</button>
                    <h2 id="comp-detail-title">Competition Name</h2>
                </div>
            </div>

            <div class="detail-grid">
                <!-- Info Box -->
                <div class="info-panel">
                    <h3 class="micro-label info-panel-title">Competition Details</h3>

                    <div class="info-field">
                        <span class="info-field-label">Name</span>
                        <div id="info-comp-name" class="info-field-value info-field-value--name">-</div>
                    </div>

                    <div class="info-field">
                        <span class="info-field-label">Type</span>
                        <div id="info-comp-type" class="info-field-value">-</div>
                    </div>

                    <div class="info-field">
                        <span class="info-field-label">Dates</span>
                        <div id="info-comp-dates" class="info-field-value">-</div>
                    </div>
                </div>

                <!-- Upload Area -->
                <div id="comp-upload-area" class="upload-area" style="flex: 1; margin: 0; display: flex; flex-direction: column; justify-content: center; align-items: center;">
                    <p class="upload-title">Drag & Drop PDF files here</p>
                    <p class="upload-or">or</p>
                    <button id="browse-files-btn" class="btn btn-sm btn-primary">Browse Files</button>
                    <input type="file" id="file-input" multiple accept=".pdf" style="display: none;">
                    <div id="upload-status" class="upload-status"></div>
                </div>
            </div>

            <div id="comp-files-container">
                <p class="text-muted">Loading files...</p>
            </div>

            <div id="action-container" class="action-container">
                 <div id="generated-files-list" class="generated-files-list">
                      <!-- Generated files injected here -->
                 </div>

                 <div id="right-panel" class="right-panel">
                      <div id="options-area" class="options-area">
                          <!-- Options injected here -->
                      </div>
                      <button id="btn-generate" class="btn btn-primary btn-generate" disabled>
                         Generate Papers
                      </button>
                 </div>
            </div>
        </div>
      </div>

      <div id="view-create-competition" class="hidden">
        <div class="card reveal" style="max-width: 600px; margin: 0 auto;">
             <span class="micro-label">New Competition</span>
             <h2>Create New Competition</h2>
             <p class="text-muted" style="margin-bottom: 1.5rem;">Enter a name for the new competition folder.</p>
             <div style="margin-bottom: 1.5rem;">
                <label class="form-label">Competition Name</label>
                <input type="text" id="comp-name-input" class="form-input" placeholder="e.g. Winter-Cup-2026">
             </div>
             <div class="form-actions">
                <button id="btn-cancel-create" class="btn btn-ghost">Cancel</button>
                <button id="btn-confirm-create" class="btn btn-primary">Create</button>
             </div>
        </div>
      </div>

      <div id="view-welcome" class="card reveal" style="max-width: 800px; margin: 0 auto;">
        <span class="micro-label">Judge Paper Creator</span>
        <h2 style="margin-bottom: 1.5rem;">Welcome to Judge Paper Creator</h2>

        <div style="margin-bottom: 2rem;">
            <h3 style="font-size: 1.15rem; margin-bottom: 0.75rem;">How to use:</h3>
            <ol class="howto-list">
                <li>Click <strong>New Competition</strong> to create a workspace for your event.</li>
                <li>Open the competition and <strong>upload the PDF files</strong> exported from <em>Figure Skating Manager</em>.</li>
                <li>The system will automatically validate the files and ensure all required documents are present.</li>
                <li>Once validated, click <strong>Generate Papers</strong> to create the combined PDF booklets and ZIP archives.</li>
                <li>Download the generated files using the links that appear. You can also copy the links to share them.</li>
            </ol>
            <button id="action-btn" class="btn btn-primary">Go to Competitions</button>
        </div>

        <div class="card-footnote">
            <p>
                <strong>Feedback & Support:</strong><br>
                Please report any bugs or send feature requests to: <a href="mailto:markus@lintuala.fi">markus@lintuala.fi</a>
            </p>
        </div>
      </div>
    </div>
  </main>
`;

// Helper to switch views
function showView(viewId: string) {
    const views = ['view-welcome', 'view-competitions', 'view-create-competition', 'view-competition-details'];
    views.forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            if (id === viewId) el.classList.remove('hidden');
            else el.classList.add('hidden');
        }
    });
}

async function init() {
  const loadingView = document.getElementById('loading-view')!;
  const errorView = document.getElementById('error-view')!;
  const landingView = document.getElementById('landing-view')!;
  const mainContent = document.getElementById('main-content')!;
  const navContainer = document.getElementById('site-nav-container')!;

  try {
    // 1. Get Auth Info (server-side endpoint reads Easy Auth headers, no tokens exposed)
    let clientPrincipal: ClientPrincipal | null = null;
    try {
        const response = await fetch('/userinfo');
        const userInfo = await response.json();

        if (userInfo && userInfo.authenticated) {
            clientPrincipal = {
                userId: userInfo.userId || '',
                identityProvider: userInfo.identityProvider || 'aad',
                userDetails: userInfo.userDetails || '',
                userRoles: userInfo.userRoles || ['authenticated', 'anonymous']
            };
        }
    } catch (_e) {
        // parsing failed, assume unauthenticated
    }

    // 2. Render the shared site nav. The in-app dropdown (Competitions /
    // New Competition) is only shown once authenticated.
    navContainer.innerHTML = renderSiteNav({
        activeApp: 'judgepapers',
        logoUrl: '/logo.png',
        ...(clientPrincipal ? {
            appNavItems: [
                { id: 'competitions', label: 'Competitions', enabled: true },
                { id: 'new-competition', label: 'New Competition', enabled: true },
            ]
        } : {})
    });
    initSiteNav();
    const userSection = document.getElementById('fst-nav-right')!;

    if (!clientPrincipal) {
        // Not authenticated
        userSection.innerHTML = `<a href="/.auth/login/aad" class="btn btn-primary btn-sm">Sign In</a>`;
        loadingView.classList.add('hidden');
        landingView.classList.remove('hidden');
        return;
    }

    // 3. Setup User Menu
    setupUserMenu(userSection, clientPrincipal);

    // 4. Auth Success - Show Content
    // We rely on SWA Entra ID authentication. If we are here, we are authenticated.
    loadingView.classList.add('hidden');
    mainContent.classList.remove('hidden');

    // Load categories cache from API (table-driven config)
    await loadCategoriesCache();

    const loadCompetitions = async () => {
        showView('view-competitions');
        const listContainer = document.getElementById('competitions-list')!;
        listContainer.innerHTML = '<p class="text-muted">Loading competitions...</p>';

        try {
            const resp = await fetch('/api/list_competitions');
            if (!resp.ok) {
                throw new Error(`Error ${resp.status}: ${resp.statusText}`);
            }
            const competitions: any[] = await resp.json();

            if (competitions.length === 0) {
                 listContainer.innerHTML = '<p class="text-muted">No competitions found. Create one to get started.</p>';
                 return;
            }
            
            listContainer.innerHTML = competitions.map(comp => {
                let dateStr = '-';
                if (comp.createdDate !== '-') {
                     try {
                         const d = new Date(comp.createdDate);
                         const dd = String(d.getDate()).padStart(2, '0');
                         const mm = String(d.getMonth() + 1).padStart(2, '0');
                         const yyyy = d.getFullYear();
                         dateStr = `${dd}.${mm}.${yyyy}`;
                     } catch(e) {}
                }

                return `
                <div class="comp-row">
                    <div class="comp-row-head">
                        <span class="comp-row-name">${escapeHtml(comp.name)}</span>
                        <div class="comp-row-actions">
                            <button class="btn btn-sm btn-ghost open-comp-btn" data-comp-id="${escapeHtml(comp.id)}" data-comp-name="${escapeHtml(comp.name)}">Open</button>
                            <button class="btn btn-sm btn-ghost btn-ghost--danger delete-comp-btn" data-comp-id="${escapeHtml(comp.id)}" data-comp-name="${escapeHtml(comp.name)}">Delete</button>
                        </div>
                    </div>
                    <div class="comp-row-meta">
                        <span>Creator: ${escapeHtml(comp.createdBy)}</span>
                        <span>Created: ${escapeHtml(dateStr)}</span>
                    </div>
                </div>
            `}).join('');
            
            // Add listeners to new buttons
            document.querySelectorAll('.open-comp-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    const el = e.currentTarget as HTMLElement;
                    openCompetition(el.dataset.compId!, el.dataset.compName!);
                });
            });

            document.querySelectorAll('.delete-comp-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    const el = e.currentTarget as HTMLElement;
                    openDeleteCompetitionModal(el.dataset.compId!, el.dataset.compName!);
                });
            });

        } catch (_e) {
            listContainer.innerHTML = `<p class="text-error">Failed to load competitions.</p>`;
        }
    };

    // List Details Logic
    let currentCompetitionData: any = null;
    let isGlobalValid = false;

    function renderCompetitionView() {
        const container = document.getElementById('comp-files-container');
        if (!container || !currentCompetitionData) return;
        
        isGlobalValid = true; // Assume true, invalidate if any issue found
        
        const structure = currentCompetitionData.structure;
        const competitionFiles = currentCompetitionData.competitionFiles || [];

        if (Object.keys(structure).length === 0 && competitionFiles.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <p>No processed files found.</p>
                    <p class="empty-hint">Upload PDFs to get started.</p>
                </div>
            `;
            isGlobalValid = false;
            updateGenerateButton();
            return;
        }

        let html = '';
        const categories = Object.keys(structure).sort();

        // Check alerts
        if (currentCompetitionData.alerts && currentCompetitionData.alerts.length > 0) {
            isGlobalValid = false;
        }

        // Competition-level validation (e.g., CompetitionSchedule required)
        const compValidation = validateCompetition(competitionFiles);
        if (!compValidation.isValid) {
            isGlobalValid = false;
            html += `
                <div class="alert-missing">
                    <h5>Missing Competition Files:</h5>
                    <ul>
                        ${compValidation.missingFiles.map(f => `<li>${f}</li>`).join('')}
                    </ul>
                </div>
            `;
        }

        // Show competition-wide files (e.g. CompetitionSchedule) in their own section
        if (competitionFiles.length > 0) {
            html += `
                <div class="category-card">
                    <div class="category-header category-header--static is-valid">
                        <div class="category-head-lead">
                            <span class="status-mark">✓</span>
                            <span class="category-title">Competition Files</span>
                        </div>
                    </div>
                    <div class="category-content">
                        <div class="file-list">
                            ${competitionFiles.map((file: any) => `
                                <div class="file-row">
                                    <span title="${escapeHtml(file.suffix)}">${escapeHtml(file.filename)}</span>
                                    <button class="file-delete-btn delete-file-btn" data-filename="${escapeHtml(file.filename)}" title="Delete File">×</button>
                                </div>
                            `).join('')}
                        </div>
                    </div>
                </div>
            `;
        }

        for (const category of categories) {
            const segments = structure[category];
            // System is always ISU for Figure Skating now
            const validation = validateCategory(segments);
            
            if (!validation.isValid) {
                isGlobalValid = false;
            }
            
            const validityClass = validation.isValid ? 'is-valid' : 'is-invalid';
            const statusIcon = validation.isValid ? '✓' : '⚠︎';
            const isCollapsed = validation.isValid;

            // Check for competition name conflict
            let catCompNameHtml = '';
            if (currentCompetitionData.alerts && currentCompetitionData.alerts.length > 0) {
                 let detectedName = '';
                 // Find comp name in any file of this category
                 for (const segment of Object.values(segments)) {
                     for (const file of (segment as any[])) {
                         if (file.competition_name) {
                             detectedName = file.competition_name;
                             break;
                         }
                     }
                     if (detectedName) break;
                 }
                 if (detectedName) {
                    catCompNameHtml = `<span class="category-conflict">Competition name: ${escapeHtml(detectedName)}</span>`;
                 }
            }
            
            const displayCategory = getLocalizedCategoryName(category, segments) || category || '(Unspecified Category)';
            
            // Check if this specific category should be tagged MUPI
            // Now driven by the categories table via the backend's judgingMethod field
            let isMupi = false;
            // Check from the file data if judgingMethod is available (enriched by backend)
            for (const segment of Object.values(segments)) {
                for (const file of (segment as any[])) {
                     if (file.judgingMethod === 'MUPI') {
                         isMupi = true;
                         break;
                     }
                }
                if (isMupi) break;
            }
            // Fallback: check via the categories cache using categoryCode
            if (!isMupi) {
                for (const segment of Object.values(segments)) {
                    for (const file of (segment as any[])) {
                        if (file.categoryCode && isMupiCategory(file.categoryCode)) {
                            isMupi = true;
                            break;
                        }
                    }
                    if (isMupi) break;
                }
            }

            html += `
                <div class="category-card">
                    <!-- Header -->
                    <div class="category-header ${validityClass}" data-category="${category}">
                        <div class="category-head-lead">
                             <span class="status-mark">${statusIcon}</span>
                             <span class="category-title">${escapeHtml(displayCategory)}</span>
                             ${isMupi ? '<span class="tag-mupi">MUPI</span>' : ''}
                             ${catCompNameHtml}
                        </div>
                        <div class="category-head-tail">
                            ${ validation.missingFiles.length > 0 ? `<span class="missing-count">${validation.missingFiles.length} missing</span>` : '' }
                            <span class="toggle-icon">${isCollapsed ? '▾' : '▴'}</span>
                        </div>
                    </div>

                    <!-- Content -->
                    <div class="category-content" id="content-${category.replace(/\s+/g, '-')}" style="display: ${isCollapsed ? 'none' : 'block'};">

                        <!-- Missing Files Warning -->
                        ${!validation.isValid ? `
                            <div class="alert-missing">
                                <h5>Missing Files:</h5>
                                <ul>
                                    ${validation.missingFiles.map(f => `<li>${f}</li>`).join('')}
                                </ul>
                            </div>
                        ` : ''}

                        <!-- Segments -->
            `;

            for (const [segment, files] of Object.entries(segments as any)) {
                html += `
                    <div class="segment-block">
                        <h4 class="segment-title">${segment}</h4>
                        <div class="file-list">
                `;

                (files as any[]).forEach((file: any) => {
                    html += `
                        <div class="file-row">
                            <span title="${escapeHtml(file.suffix)}">${escapeHtml(file.filename)}</span>
                            <button class="file-delete-btn delete-file-btn" data-filename="${escapeHtml(file.filename)}" title="Delete File">×</button>
                        </div>
                    `;
                });

                html += `</div></div>`;
            }
            
            html += `</div></div>`; // Close category-content and outer div
        }
        
        container.innerHTML = html;
        
        // Attach delete-file-btn event listeners (replaces inline onclick)
        document.querySelectorAll('.delete-file-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const filename = (e.currentTarget as HTMLElement).dataset.filename!;
                (window as any).promptDeleteFile(filename);
            });
        });

        // Header click logic
        document.querySelectorAll('.category-header').forEach(header => {
            header.addEventListener('click', (e) => {
                const cat = (e.currentTarget as HTMLElement).getAttribute('data-category');
                if (cat) {
                     const content = document.getElementById(`content-${cat.replace(/\s+/g, '-')}`);
                     if (content) {
                         const isHidden = content.style.display === 'none';
                         content.style.display = isHidden ? 'block' : 'none';
                         
                         // Update arrow
                         const arrow = (e.currentTarget as HTMLElement).querySelector('.toggle-icon');
                         if(arrow) arrow.textContent = isHidden ? '▴' : '▾';
                     }
                }
            });
        });
        
        updateGenerateButton();
    }

    function updateGenerateButton() {
        const btn = document.getElementById('btn-generate') as HTMLButtonElement;
        if (btn) {
            btn.disabled = !isGlobalValid;
        }
    }

    // Generate Handler
    document.getElementById('btn-generate')?.addEventListener('click', async () => {
        if (!currentCompetitionData || !isGlobalValid) return;
        
        const btn = document.getElementById('btn-generate') as HTMLButtonElement;
        const originalText = 'Generate Papers';
        
        // Start Loading
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span>Generating...';

        // Collect Options
        // Toggle Logic Inversion:
        // UI Checkbox = "Use Time Schedule"
        // Backend 'segmentCover' = "Use Generated Cover Page"
        // So: Checked (Time Schedule) -> False (No Generated Cover)
        //     Unchecked (Cover Page)  -> True (Generated Cover)
        
        const globalToggle = document.getElementById('global-use-time-schedule') as HTMLInputElement;
        const segmentToggles = document.querySelectorAll('.time-schedule-toggle') as NodeListOf<HTMLInputElement>;
        
        const options: any = {
            globalSegmentCover: globalToggle ? !globalToggle.checked : false,
            segmentCovers: {},
            language: currentLanguage
        };
        
        if (segmentToggles) {
            segmentToggles.forEach(t => {
                const prefix = t.getAttribute('data-prefix');
                if (prefix) {
                    options.segmentCovers[prefix] = !t.checked;
                }
            });
        }
        
        try {
            const resp = await fetch('/api/generate_judging_papers', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    workingFolder: currentCompetitionData.id,
                    options: options
                })
            });

            if (resp.ok) {
                 // Success Animation
                 btn.innerHTML = 'Completed!';
                 btn.classList.add('btn-success');
                 
                 setTimeout(() => {
                     btn.innerHTML = originalText;
                     btn.classList.remove('btn-success');
                     btn.disabled = false;
                     loadCompetitionDetails(currentCompetitionData.id);
                 }, 3000);
            } else {
                const errText = await resp.text();
                openErrorModal('Generation Failed', `An error occurred while generating papers:<br><br>${errText}`);
                // Reset immediately on error
                btn.innerHTML = originalText;
                btn.disabled = false; 
            }
        } catch (_e) {
            openErrorModal('Generation Error', 'Network error or server unreachable.');
            btn.innerHTML = originalText;
            btn.disabled = false;
        }
    });

    // Global handler
    (window as any).promptDeleteFile = (filename: string) => {
         if (!currentCompetitionData) return;
         openDeleteFileModal(filename, currentCompetitionData.id);
    };

    async function loadCompetitionDetails(id: string) {
        const container = document.getElementById('comp-files-container')!;
        container.innerHTML = '<p class="text-muted">Scanning files...</p>';

        try {
            const resp = await fetch(`/api/get_competition_details?id=${encodeURIComponent(id)}`);
            if (!resp.ok) throw new Error('Failed to load details');
            
            const data = await resp.json();
            currentCompetitionData = data;
            
            // Update Info Box with extracted data
            if (data.fullName && data.fullName !== '-') {
                 document.getElementById('info-comp-name')!.textContent = data.fullName;
            } else {
                 document.getElementById('info-comp-name')!.textContent = data.name; // Fallback to folder name
            }

            // Process metadata
            document.getElementById('info-comp-type')!.textContent = data.type || '-';
            
            document.getElementById('info-comp-dates')!.textContent = data.date || '-';
            
            const nameEl = document.getElementById('info-comp-name')!;

            // Check for alerts
            if (data.alerts && data.alerts.length > 0) {
                 const alertMsg = data.alerts.join('<br>');
                 openErrorModal('Configuration Error', alertMsg);

                 nameEl.textContent = 'Error! Multiple file names found! FIX THESE!';
                 nameEl.classList.add('info-field-value--alert');
            } else {
                 nameEl.classList.remove('info-field-value--alert');
            }
            
            // Set language from competition settings
            currentLanguage = data.language || 'fi';
            
            updateOptionsView(data);
            renderCompetitionView();

            // Render Generated Files
            const genContainer = document.getElementById('generated-files-list');
            if (genContainer) {
                 if (data.generatedFiles && data.generatedFiles.length > 0) {
                     genContainer.innerHTML = data.generatedFiles.map((f: any) => {
                         let dateDisplay = '';
                         try {
                             // Try to extract date from filename (YYYYMMDD)
                             const match = f.fileName.match(/(\d{4})(\d{2})(\d{2})/);
                             if (match) {
                                 const [_, y, m, d] = match;
                                 dateDisplay = `${d}.${m}.${y}`;
                             }
                         } catch (e) {}
                         
                         // Expiration
                         let expStr = '-';
                         try {
                              const expDate = new Date(f.expiration);
                              const dd = String(expDate.getDate()).padStart(2, '0');
                              const mm = String(expDate.getMonth() + 1).padStart(2, '0');
                              const yyyy = expDate.getFullYear();
                              expStr = `${dd}.${mm}.${yyyy}`;
                         } catch(e) {}
                         
                         // Size
                         let sizeStr = '';
                         if (f.size) {
                             const size = parseInt(f.size);
                             if (size > 1024 * 1024) {
                                  sizeStr = (size / (1024 * 1024)).toFixed(1) + ' MB';
                             } else {
                                  sizeStr = (size / 1024).toFixed(0) + ' KB';
                             }
                         }

                         const safeUrl = f.url;
                         const safeFileName = escapeHtml(f.fileName);
                         const safeDescription = escapeHtml(f.description);
                         const safeJudgePapersPath = escapeHtml('judgePapers/' + f.fileName);

                         return `
                            <div class="gen-file">
                                <a href="${safeUrl}" target="_blank" rel="noopener noreferrer" class="gen-file-link">
                                    <div class="gen-file-head">
                                        <span class="gen-file-desc">
                                            ${safeDescription}
                                            ${dateDisplay ? `<span class="gen-file-date">${escapeHtml(dateDisplay)}</span>` : ''}
                                        </span>
                                        <div class="gen-file-badges">
                                            ${sizeStr ? `<span class="gen-badge">${escapeHtml(sizeStr)}</span>` : ''}
                                            <span class="gen-badge">Exp: ${escapeHtml(expStr)}</span>
                                        </div>
                                    </div>
                                    <div class="gen-file-name">
                                        ${safeFileName}
                                    </div>
                                </a>
                                <button class="icon-btn icon-btn--copy copy-link-btn" data-url="${safeUrl}" title="Copy Link to Clipboard">
                                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                                </button>
                                <button class="icon-btn icon-btn--danger delete-gen-file-btn" data-filename="${safeJudgePapersPath}" title="Delete File">
                                    ×
                                </button>
                            </div>
                         `;
                     }).join('');

                     // Attach copy-link event listeners
                     document.querySelectorAll('.copy-link-btn').forEach(btn => {
                         btn.addEventListener('click', (e) => {
                             e.stopPropagation();
                             const url = (e.currentTarget as HTMLElement).dataset.url!;
                             const el = e.currentTarget as HTMLElement;
                             navigator.clipboard.writeText(url).then(() => {
                                 const originalHTML = el.innerHTML;
                                 el.innerHTML = '✓';
                                 el.classList.add('is-copied');
                                 setTimeout(() => {
                                     el.innerHTML = originalHTML;
                                     el.classList.remove('is-copied');
                                 }, 2000);
                             }).catch(() => {});
                         });
                     });

                     // Attach delete-gen-file event listeners
                     document.querySelectorAll('.delete-gen-file-btn').forEach(btn => {
                         btn.addEventListener('click', () => {
                             const filename = (btn as HTMLElement).dataset.filename!;
                             (window as any).promptDeleteFile(filename);
                         });
                     });
                 } else {
                     genContainer.innerHTML = '';
                 }
            }

        } catch (_e) {
            container.innerHTML = '<p class="text-error">Error loading files.</p>';
        }
    }

    function updateOptionsView(data: any) {
        const optionsArea = document.getElementById('options-area');
        if (!optionsArea) return;

        // Determine if Figure Skating
        // If Figure Skating, default "Use Time Schedule" to FALSE (i.e. use Cover Page)
        const isFigureSkating = (data.type && data.type.includes('Figure skating')) || false;
        const defaultUseTimeSchedule = !isFigureSkating;
        
        // Find all segments
        const segments: { prefix: string, label: string }[] = [];
        
        // structure: { category: { segment: [files] } }
        if (data.structure) {
            for (const cat in data.structure) {
                if (cat === 'Uncategorized') continue;

                // Lookup friendly name using localized category name from file data
                const friendlyCatName = getLocalizedCategoryName(cat, data.structure[cat]);

                for (const seg in data.structure[cat]) {
                    const files = data.structure[cat][seg];
                    const startList = files.find((f: any) => f.suffix.includes('StartListwithTimes'));
                    if (startList) {
                        // Extract prefix from filename
                        const prefix = startList.filename.replace('_StartListwithTimes.pdf', '');
                        segments.push({
                            prefix: prefix,
                            label: `${friendlyCatName} - ${seg}`
                        });
                    }
                }
            }
        }

        if (segments.length === 0) {
             optionsArea.innerHTML = '<p class="text-muted" style="font-size: 0.9rem; margin: 0;">No segments detected yet.</p>';
             return;
        }

        optionsArea.innerHTML = `
            <h3 class="options-title">Global Settings</h3>

            <div class="option-row">
                <label class="option-check">
                    <input type="checkbox" id="use-english-names" ${currentLanguage === 'en' ? 'checked' : ''}>
                    <span>Use English category names</span>
                </label>
            </div>

            <div class="option-row">
                <label class="option-check">
                    <input type="checkbox" id="global-use-time-schedule" ${defaultUseTimeSchedule ? 'checked' : ''}>
                    <span>Use Time Schedule as a Segment cover page</span>
                </label>
            </div>

            <div class="option-advanced">
                <button id="toggle-advanced-options" class="btn btn-ghost btn-xs" style="padding-left: 0;">Show Per-Segment Settings ▸</button>
                <div id="segment-options-list" class="segment-options-list hidden">
                    ${segments.map(s => `
                        <label class="segment-option">
                            <input type="checkbox" class="time-schedule-toggle" data-prefix="${s.prefix}" ${defaultUseTimeSchedule ? 'checked' : ''}>
                            <span>${s.label}</span>
                        </label>
                    `).join('')}
                </div>
            </div>
        `;

        // Logic for Toggles
        const globalToggle = document.getElementById('global-use-time-schedule') as HTMLInputElement;
        const segmentToggles = document.querySelectorAll('.time-schedule-toggle') as NodeListOf<HTMLInputElement>;
        const listContainer = document.getElementById('segment-options-list')!;
        const toggleBtn = document.getElementById('toggle-advanced-options')!;

        // Language toggle
        const langToggle = document.getElementById('use-english-names') as HTMLInputElement;
        langToggle?.addEventListener('change', async () => {
            currentLanguage = langToggle.checked ? 'en' : 'fi';
            // Persist language to backend
            if (currentCompetitionData) {
                try {
                    await fetch('/api/save_competition_settings', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            id: currentCompetitionData.id,
                            settings: { language: currentLanguage }
                        })
                    });
                } catch (_e) {
                    console.warn('Failed to save language setting');
                }
            }
            // Re-render to reflect new language
            updateOptionsView(data);
            renderCompetitionView();
        });

        globalToggle.addEventListener('change', () => {
             const isChecked = globalToggle.checked;
             segmentToggles.forEach(t => t.checked = isChecked);
        });
        
        // Listen to individual toggles to update global
        segmentToggles.forEach(t => {
            t.addEventListener('change', () => {
                const allChecked = Array.from(segmentToggles).every(Toggle => Toggle.checked);
                if (!t.checked) {
                    globalToggle.checked = false;
                } else if (allChecked) {
                    globalToggle.checked = true;
                }
            });
        });
        
        toggleBtn.addEventListener('click', () => {
            const isHidden = listContainer.classList.contains('hidden');
            if (isHidden) {
                listContainer.classList.remove('hidden');
                toggleBtn.textContent = 'Hide Per-Segment Settings ▾';
            } else {
                listContainer.classList.add('hidden');
                toggleBtn.textContent = 'Show Per-Segment Settings ▸';
            }
        });
    }

    async function handleFiles(files: FileList, competitionId: string) {
        const statusEl = document.getElementById('upload-status')!;
        let successCount = 0;
        let errors: string[] = [];
        
        statusEl.innerHTML = `<span style="color: var(--text-secondary);">Uploading ${files.length} files...</span>`;
        
        for (let i = 0; i < files.length; i++) {
            const file = files[i];
            if (file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf')) {
                errors.push(`${file.name}: Not a PDF`);
                continue;
            }
            
            try {
                const url = `/api/upload_file?competition=${encodeURIComponent(competitionId)}&filename=${encodeURIComponent(file.name)}`;
                const resp = await fetch(url, {
                    method: 'POST',
                    body: file 
                });
                
                if (resp.ok) {
                    successCount++;
                } else {
                    errors.push(`${file.name}: Upload failed`);
                }
            } catch (e) {
                errors.push(`${file.name}: Error`);
            }
        }
        
        let msg = `<span style="color: var(--success-color);">Uploaded ${successCount} files.</span>`;
        if (errors.length > 0) {
            msg += ` <span style="color: var(--error-color);">Errors: ${errors.length}</span>`;
        }
        statusEl.innerHTML = msg;
        
        if (successCount > 0) {
             loadCompetitionDetails(competitionId);
        }
    }

    async function openCompetition(id: string, name: string) {
        showView('view-competition-details');
        const titleEl = document.getElementById('comp-detail-title')!;

        titleEl.textContent = name;
        
        // Update Info Box
        document.getElementById('info-comp-name')!.textContent = name;
        // Placeholder values for now
        document.getElementById('info-comp-type')!.textContent = '-'; 
        document.getElementById('info-comp-dates')!.textContent = '-';
        
        // Setup Upload Area
        const dropArea = document.getElementById('comp-upload-area')!;
        const fileInput = document.getElementById('file-input') as HTMLInputElement;
        const browseBtn = document.getElementById('browse-files-btn')!;
        const statusEl = document.getElementById('upload-status')!;

        // Reset status
        statusEl.innerHTML = '';
        statusEl.className = '';

        browseBtn.onclick = (e) => { e.preventDefault(); fileInput.click(); };
        
        fileInput.onchange = async (e) => {
            const files = (e.target as HTMLInputElement).files;
            if (files && files.length > 0) await handleFiles(files, id);
            fileInput.value = '';
        };
        
        dropArea.ondragover = (e) => { e.preventDefault(); dropArea.classList.add('dragover'); };
        dropArea.ondragleave = (e) => { e.preventDefault(); dropArea.classList.remove('dragover'); };
        dropArea.ondrop = (e) => {
            e.preventDefault();
            dropArea.classList.remove('dragover');
            if (e.dataTransfer && e.dataTransfer.files.length > 0) {
                handleFiles(e.dataTransfer.files, id);
            }
        };

        // Initialize Options Area if not present
        let optionsArea = document.getElementById('options-area');
        // No longer dynamically creating, as it is in the static template now
        if (optionsArea) optionsArea.innerHTML = ''; // Clear old on load

        loadCompetitionDetails(id);
    }

    document.getElementById('btn-back-list')?.addEventListener('click', loadCompetitions);

    // Modal Logic
    const modalOverlay = document.getElementById('modal-overlay')!;
    const modalTitle = document.getElementById('modal-title')!;
    const modalMessage = document.getElementById('modal-message')!;
    const modalExtra = document.getElementById('modal-extra-content')!;
    const modalCancel = document.getElementById('modal-cancel')!;
    const modalConfirm = document.getElementById('modal-confirm')!;

    type DeleteAction =
        | { type: 'COMPETITION', id: string, name: string }
        | { type: 'FILE', filename: string, competition: string };

    let pendingAction: DeleteAction | null = null;

    function openDeleteCompetitionModal(compId: string, compName: string) {
        pendingAction = { type: 'COMPETITION', id: compId, name: compName };
        modalTitle.textContent = `Delete Competition?`;
        modalMessage.innerHTML = `Are you sure you want to delete <strong>${escapeHtml(compName)}</strong>?<br>This action cannot be undone and will permanently remove all associated files.`;
        
        // Add Checkbox
        modalExtra.innerHTML = `
            <label class="modal-check">
                <input type="checkbox" id="confirm-delete-checkbox" style="margin-top: 0.25rem;">
                <span>I understand that this action is permanent.</span>
            </label>
        `;

        modalConfirm.textContent = 'Delete';
        modalConfirm.classList.remove('btn-primary');
        modalConfirm.classList.add('btn-danger');
        (modalConfirm as HTMLButtonElement).disabled = true;

        // Checkbox listener
        const checkbox = document.getElementById('confirm-delete-checkbox') as HTMLInputElement;
        checkbox.addEventListener('change', (e) => {
             (modalConfirm as HTMLButtonElement).disabled = !(e.target as HTMLInputElement).checked;
        });

        modalOverlay.classList.remove('hidden');
    }

    function openDeleteFileModal(filename: string, competition: string) {
        pendingAction = { type: 'FILE', filename, competition };
        modalTitle.textContent = `Delete File?`;
        modalMessage.innerHTML = `Are you sure you want to delete <strong>${escapeHtml(filename)}</strong>?`;
        modalExtra.innerHTML = ''; // No checkbox for single file

        modalConfirm.textContent = 'Delete';
        modalConfirm.classList.remove('btn-primary');
        modalConfirm.classList.add('btn-danger');
        (modalConfirm as HTMLButtonElement).disabled = false;

        modalOverlay.classList.remove('hidden');
    }

    function openErrorModal(title: string, message: string) {
        modalTitle.textContent = title;
        modalMessage.innerHTML = message;
        modalExtra.innerHTML = '';
        
        modalCancel.classList.add('hidden'); // Hide cancel
        modalConfirm.textContent = 'OK';
        modalConfirm.classList.remove('btn-danger', 'btn-primary'); 
        modalConfirm.classList.add('btn-primary');
        (modalConfirm as HTMLButtonElement).disabled = false;
        
        pendingAction = null; // No action to take on confirm
        
        modalOverlay.classList.remove('hidden');
    }

    modalCancel.addEventListener('click', () => {
        modalOverlay.classList.add('hidden');
        pendingAction = null;
    });

    modalConfirm.addEventListener('click', async () => {
        if (!pendingAction) {
            // Just close if no action (e.g. Alert mode)
            modalOverlay.classList.add('hidden');
            // Restore Cancel button capability
            modalCancel.classList.remove('hidden'); 
            return;
        }
        
        const btn = modalConfirm as HTMLButtonElement;
        const originalText = btn.textContent;
        btn.textContent = 'Deleting...';
        btn.disabled = true;

        try {
            if (pendingAction.type === 'COMPETITION') {
                const resp = await fetch(`/api/delete_competition?id=${encodeURIComponent(pendingAction.id)}`);
                if (resp.ok) {
                     modalOverlay.classList.add('hidden');
                     loadCompetitions(); 
                } else {
                    alert('Failed to delete competition');
                }
            } else if (pendingAction.type === 'FILE') {
                const resp = await fetch(`/api/delete_file?competition=${encodeURIComponent(pendingAction.competition)}&filename=${encodeURIComponent(pendingAction.filename)}`, {
                    method: 'DELETE'
                });
                if (resp.ok) {
                    modalOverlay.classList.add('hidden');
                    if (currentCompetitionData) loadCompetitionDetails(currentCompetitionData.id);
                } else {
                    alert('Failed to delete file.');
                }
            }
        } catch (_error) {
            alert('Error deleting item');
        } finally {
            btn.textContent = originalText;
            btn.disabled = false;
        }
    });


    // Shared-nav dropdown items (rendered by renderSiteNav with data-nav-action)
    document.querySelectorAll<HTMLElement>('[data-nav-action]').forEach(el => {
        el.addEventListener('click', (e) => {
            e.preventDefault();
            // Close the nav dropdown (its own click handler stops propagation)
            document.querySelectorAll('.fst-dropdown-menu').forEach(m => m.classList.remove('fst-dropdown-menu--open'));
            document.querySelectorAll('.fst-nav-item-btn[data-dropdown]').forEach(t => t.setAttribute('aria-expanded', 'false'));

            const action = (e.currentTarget as HTMLElement).dataset.navAction;
            if (action === 'competitions') loadCompetitions();
            else if (action === 'new-competition') showView('view-create-competition');
        });
    });


    document.getElementById('action-btn')?.addEventListener('click', loadCompetitions);
    document.getElementById('btn-create-comp')?.addEventListener('click', () => showView('view-create-competition'));
    
    document.getElementById('btn-cancel-create')?.addEventListener('click', loadCompetitions);
    document.getElementById('btn-confirm-create')?.addEventListener('click', async () => {
        const input = document.getElementById('comp-name-input') as HTMLInputElement;
        const name = input.value.trim();
        
        if (!name) {
            alert('Please enter a competition name');
            return;
        }

        const btn = document.getElementById('btn-confirm-create') as HTMLButtonElement;
        const originalText = btn.textContent;
        btn.textContent = 'Creating...';
        btn.disabled = true;

        try {
            const resp = await fetch(`/api/create_competition?name=${encodeURIComponent(name)}`);
            if (resp.ok) {
                input.value = ''; // Reset
                loadCompetitions();
            } else {
                const text = await resp.text();
                alert('Failed to create competition: ' + text);
            }
        } catch (_e) {
            alert('Error creating competition');
        } finally {
            btn.textContent = originalText;
            btn.disabled = false;
        }
    });


  } catch (_err) {
      loadingView.classList.add('hidden');
      errorView.classList.remove('hidden');
      errorView.innerHTML = `<h2>Error</h2><p>Failed to initialize application.</p>`;
  }
}

function setupUserMenu(container: HTMLElement, user: ClientPrincipal) {
    container.innerHTML = `
        <div class="user-menu-container">
            <button id="user-menu-btn" class="user-btn">
                <span>${escapeHtml(user.userDetails)}</span>
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16">
                    <path fill-rule="evenodd" d="M1.646 4.646a.5.5 0 0 1 .708 0L8 10.293l5.646-5.647a.5.5 0 0 1 .708.708l-6 6a.5.5 0 0 1-.708 0l-6-6a.5.5 0 0 1 0-.708z"/>
                </svg>
            </button>
            <div id="user-dropdown" class="dropdown-menu">
                <div class="dropdown-header">
                    Signed in as <br> <strong>${escapeHtml(user.userDetails)}</strong>
                </div>
                <a href="/.auth/logout?post_logout_redirect_uri=/" class="dropdown-item">Sign Out</a>
            </div>
        </div>
    `;

    const btn = document.getElementById('user-menu-btn')!;
    const dropdown = document.getElementById('user-dropdown')!;

    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        dropdown.classList.toggle('show');
    });

    document.addEventListener('click', () => {
        dropdown.classList.remove('show');
    });

    dropdown.addEventListener('click', (e) => {
        e.stopPropagation();
    });
}

// Start
init();
