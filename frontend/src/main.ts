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
      <p style="color: var(--text-secondary);">Please wait while we verify your credentials.</p>
    </div>

    <div id="error-view" class="error-screen hidden">
        <!-- Error content injected dynamically -->
    </div>

    <div id="landing-view" class="hidden">
      <div class="card" style="text-align: center; max-width: 800px; margin: 4rem auto;">
        <h2 style="font-size: 2rem; margin-bottom: 1.5rem;">Create Judging Papers with Ease</h2>
        <p style="margin-bottom: 1.5rem; color: var(--text-secondary); font-size: 1.1rem; line-height: 1.6;">
          This application provides an easy way to create judging papers for figure skating competitions.
          Simply upload the PDF exports from <strong>Figure Skating Manager</strong>, and we handle the rest.
        </p>
        <div style="margin: 2rem 0; padding: 1.5rem; background-color: #f8fafc; border-radius: 0.5rem; border: 1px solid var(--border-color);">
            <p style="color: var(--text-secondary); margin-bottom: 1rem;">
                To access the application, please contact the administrator:
            </p>
             <a href="mailto:markus@lintuala.fi" style="color: var(--primary-color); font-weight: 600;">markus@lintuala.fi</a>
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
            <h3 id="modal-title" style="margin-top: 0; font-size: 1.25rem;">Confirm Action</h3>
            <p id="modal-message" style="color: var(--text-secondary); margin-bottom: 1.5rem;">Are you sure?</p>
            
            <div id="modal-extra-content" style="margin-bottom: 1.5rem;">
                <!-- Dynamic Content like Checkbox -->
            </div>

            <div style="display: flex; justify-content: flex-end; gap: 0.75rem;">
                <button id="modal-cancel" class="btn btn-ghost btn-sm">Cancel</button>
                <button id="modal-confirm" class="btn btn-primary btn-sm">Confirm</button>
            </div>
        </div>
      </div>

      <div id="view-competitions" class="hidden">
        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem;">
                <h2>Competitions</h2>
                <button id="btn-create-comp" class="btn btn-primary btn-sm">Create New</button>
            </div>
            <div id="competitions-list">
                <p style="color: var(--text-secondary);">Loading competitions...</p>
            </div>
        </div>
      </div>

      <div id="view-competition-details" class="hidden">
        <div class="card">
             <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem;">
                <div style="display: flex; align-items: center; gap: 1rem;">
                    <button id="btn-back-list" class="btn btn-sm btn-ghost">← Back</button>
                    <h2 id="comp-detail-title" style="margin: 0;">Competition Name</h2>
                </div>
            </div>

            <div style="display: flex; gap: 1.5rem; margin-bottom: 2rem; align-items: stretch;">
                <!-- Info Box -->
                <div style="width: 250px; background: #f8fafc; padding: 1.5rem; border-radius: 0.5rem; border: 1px solid var(--border-color); flex-shrink: 0;">
                    <h3 style="margin-top: 0; font-size: 0.875rem; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 1.25rem; font-weight: 600;">Competition Details</h3>
                    
                    <div style="margin-bottom: 1.25rem;">
                        <label style="display: block; font-size: 0.75rem; color: var(--text-secondary); margin-bottom: 0.25rem;">Name</label>
                        <div id="info-comp-name" style="font-weight: 600; font-size: 1rem; word-break: break-word; line-height: 1.3;">-</div>
                    </div>
                    
                    <div style="margin-bottom: 1.25rem;">
                        <label style="display: block; font-size: 0.75rem; color: var(--text-secondary); margin-bottom: 0.25rem;">Type</label>
                        <div id="info-comp-type" style="font-weight: 500; font-size: 0.95rem;">-</div>
                    </div>
                    
                    <div>
                        <label style="display: block; font-size: 0.75rem; color: var(--text-secondary); margin-bottom: 0.25rem;">Dates</label>
                        <div id="info-comp-dates" style="font-weight: 500; font-size: 0.95rem;">-</div>
                    </div>
                </div>

                <!-- Upload Area -->
                <div id="comp-upload-area" class="upload-area" style="flex: 1; margin: 0; display: flex; flex-direction: column; justify-content: center; align-items: center;">
                    <p style="margin: 0 0 0.5rem 0; font-size: 1.1rem; font-weight: 500;">Drag & Drop PDF files here</p>
                    <p style="margin: 0 0 1rem 0; color: var(--text-secondary);">or</p>
                    <button id="browse-files-btn" class="btn btn-sm btn-primary">Browse Files</button>
                    <input type="file" id="file-input" multiple accept=".pdf" style="display: none;">
                    <div id="upload-status" style="margin-top: 1rem; font-size: 0.875rem; min-height: 1.25rem;"></div>
                </div>
            </div>
            
            <div id="comp-files-container">
                <p style="color: var(--text-secondary);">Loading files...</p>
            </div>
            
            <div id="action-container" style="margin-top: 2rem; border-top: 1px solid var(--border-color); padding-top: 1.5rem; display: flex; gap: 2rem; align-items: flex-start;">
                 <div id="generated-files-list" style="flex: 1; display: flex; flex-direction: column; gap: 0.5rem;">
                      <!-- Generated files injected here -->
                 </div>
                 
                 <div id="right-panel" style="width: 400px; flex-shrink: 0; display: flex; flex-direction: column; gap: 1rem;">
                      <div id="options-area" style="background: #f8fafc; border: 1px solid var(--border-color); border-radius: 0.5rem; padding: 1rem;">
                          <!-- Options injected here -->
                      </div>
                      <button id="btn-generate" class="btn btn-primary" style="width: 100%; display: flex; align-items: center; justify-content: center; height: 3rem;" disabled>
                         Generate Papers
                      </button>
                 </div>
            </div>
        </div>
      </div>
      
      <div id="view-create-competition" class="hidden">
        <div class="card" style="max-width: 600px; margin: 0 auto;">
             <h2>Create New Competition</h2>
             <p style="color: var(--text-secondary); margin-bottom: 1.5rem;">Enter a name for the new competition folder.</p>
             <div style="margin-bottom: 1rem;">
                <label style="display: block; margin-bottom: 0.5rem; font-weight: 500;">Competition Name</label>
                <input type="text" id="comp-name-input" style="width: 100%; padding: 0.75rem; border: 1px solid var(--border-color); border-radius: 0.5rem; box-sizing: border-box;" placeholder="e.g. Winter-Cup-2026">
             </div>
             <div style="display: flex; justify-content: flex-end; gap: 1rem;">
                <button id="btn-cancel-create" class="btn btn-ghost">Cancel</button>
                <button id="btn-confirm-create" class="btn btn-primary">Create</button>
             </div>
        </div>
      </div>
      
      <div id="view-welcome" class="card" style="max-width: 800px; margin: 0 auto;">
        <h2 style="margin-bottom: 1.5rem;">Welcome to Judge Paper Creator</h2>
        
        <div style="margin-bottom: 2rem;">
            <h3 style="font-size: 1.1rem; color: #0c4a6e; margin-bottom: 0.75rem;">How to use:</h3>
            <ol style="color: var(--text-secondary); line-height: 1.6; padding-left: 1.5rem; margin-bottom: 1.5rem;">
                <li>Click <strong>New Competition</strong> to create a workspace for your event.</li>
                <li>Open the competition and <strong>upload the PDF files</strong> exported from <em>Figure Skating Manager</em>.</li>
                <li>The system will automatically validate the files and ensure all required documents are present.</li>
                <li>Once validated, click <strong>Generate Papers</strong> to create the combined PDF booklets and ZIP archives.</li>
                <li>Download the generated files using the links that appear. You can also copy the links to share them.</li>
            </ol>
            <button id="action-btn" class="btn btn-primary">Go to Competitions</button>
        </div>
        
        <div style="padding-top: 1.5rem; border-top: 1px solid var(--border-color);">
            <p style="color: var(--text-secondary); font-size: 0.9rem;">
                <strong>Feedback & Support:</strong><br>
                Please report any bugs or send feature requests to: <a href="mailto:markus@lintuala.fi" style="color: var(--primary-color);">markus@lintuala.fi</a>
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
        listContainer.innerHTML = '<p style="color: var(--text-secondary);">Loading competitions...</p>';
        
        try {
            const resp = await fetch('/api/list_competitions');
            if (!resp.ok) {
                throw new Error(`Error ${resp.status}: ${resp.statusText}`);
            }
            const competitions: any[] = await resp.json();
            
            if (competitions.length === 0) {
                 listContainer.innerHTML = '<p style="color: var(--text-secondary);">No competitions found. Create one to get started.</p>';
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
                <div style="background: var(--bg-color); padding: 1rem; margin-bottom: 0.75rem; border-radius: 0.5rem; border: 1px solid var(--border-color);">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
                        <span style="font-weight: 600; font-size: 1.1rem;">${escapeHtml(comp.name)}</span>
                        <div style="display: flex; gap: 0.5rem;">
                            <button class="btn btn-sm btn-ghost open-comp-btn" data-comp="${escapeHtml(comp.name)}">Open</button>
                            <button class="btn btn-sm btn-ghost delete-comp-btn" style="color: #ef4444;" data-comp="${escapeHtml(comp.name)}">Delete</button>
                        </div>
                    </div>
                    <div style="font-size: 0.85rem; color: var(--text-secondary); display: flex; gap: 1.5rem;">
                        <span>Creator: ${escapeHtml(comp.createdBy)}</span>
                        <span>Created: ${escapeHtml(dateStr)}</span>
                    </div>
                </div>
            `}).join('');
            
            // Add listeners to new buttons
            document.querySelectorAll('.open-comp-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    const comp = (e.currentTarget as HTMLElement).dataset.comp!;
                    openCompetition(comp);
                });
            });

            document.querySelectorAll('.delete-comp-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    const comp = (e.currentTarget as HTMLElement).dataset.comp!;
                    openDeleteCompetitionModal(comp);
                });
            });

        } catch (_e) {
            listContainer.innerHTML = `<p style="color: #ef4444;">Failed to load competitions.</p>`;
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
                <div style="text-align: center; padding: 3rem; border: 2px dashed var(--border-color); border-radius: 1rem; color: var(--text-secondary);">
                    <p>No processed files found.</p>
                    <p style="font-size: 0.875rem;">Upload PDFs to get started.</p>
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
                <div style="margin-bottom: 1rem; padding: 0.75rem; background: #fef2f2; border: 1px solid #fee2e2; border-radius: 0.5rem;">
                    <h5 style="margin: 0 0 0.5rem 0; color: var(--error-color); font-size: 0.875rem;">Missing Competition Files:</h5>
                    <ul style="margin: 0; padding-left: 1.5rem; color: #b91c1c; font-size: 0.875rem;">
                        ${compValidation.missingFiles.map(f => `<li>${f}</li>`).join('')}
                    </ul>
                </div>
            `;
        }

        // Show competition-wide files (e.g. CompetitionSchedule) in their own section
        if (competitionFiles.length > 0) {
            html += `
                <div style="margin-bottom: 0.5rem; border: 1px solid var(--border-color); border-radius: 0.5rem; overflow: hidden; background: white;">
                    <div style="background: #f1f5f9; padding: 0.5rem 1rem; display: flex; justify-content: space-between; align-items: center; border-left: 4px solid var(--success-color); min-height: 2.5rem;">
                        <div style="display: flex; align-items: center; gap: 0.75rem;">
                            <span style="color: var(--text-secondary); font-weight: bold;">📋</span>
                            <span style="font-weight: 600;">Competition Files</span>
                        </div>
                    </div>
                    <div style="padding: 1rem;">
                        <div style="display: flex; flex-direction: column; gap: 0.5rem;">
                            ${competitionFiles.map((file: any) => `
                                <div style="padding: 0.5rem; background: var(--bg-color); border-radius: 0.25rem; font-size: 0.875rem; display: flex; align-items: center; justify-content: space-between;">
                                    <span title="${escapeHtml(file.suffix)}">${escapeHtml(file.filename)}</span>
                                    <button class="btn-icon-danger delete-file-btn" data-filename="${escapeHtml(file.filename)}" style="background:none; border:none; color: #ef4444; cursor: pointer; font-size: 1.2rem; line-height: 1;" title="Delete File">×</button>
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
            
            const statusColor = validation.isValid ? 'var(--success-color)' : 'var(--error-color)';
            const statusIcon = validation.isValid ? '✓' : '⚠️';
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
                    catCompNameHtml = `<span style="color: #ef4444; font-size: 0.8rem; margin-left: 1rem; font-weight: 600;">Competition name: ${escapeHtml(detectedName)}</span>`;
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
                <div style="margin-bottom: 0.5rem; border: 1px solid var(--border-color); border-radius: 0.5rem; overflow: hidden; background: white;">
                    <!-- Header -->
                    <div class="category-header" data-category="${category}" style="background: #f1f5f9; padding: 0.5rem 1rem; cursor: pointer; display: flex; justify-content: space-between; align-items: center; border-left: 4px solid ${statusColor}; min-height: 2.5rem;">
                        <div style="display: flex; align-items: center; gap: 0.75rem; flex: 1; overflow: hidden;">
                             <span style="color: ${statusColor}; font-weight: bold; flex-shrink: 0;">${statusIcon}</span>
                             <span style="font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${escapeHtml(displayCategory)}</span>
                             ${isMupi ? '<span class="tag-mupi">MUPI</span>' : ''}
                             ${catCompNameHtml}
                        </div>
                        <div style="display: flex; align-items: center; gap: 1rem; flex-shrink: 0;">
                            ${ validation.missingFiles.length > 0 ? `<span style="font-size: 0.75rem; color: var(--error-color); font-weight: 500;">${validation.missingFiles.length} missing</span>` : '' }
                            <span class="toggle-icon" style="font-size: 0.75rem; color: var(--text-secondary); transition: transform 0.2s;">${isCollapsed ? '▼' : '▲'}</span>
                        </div>
                    </div>
                    
                    <!-- Content -->
                    <div class="category-content" id="content-${category.replace(/\s+/g, '-')}" style="padding: 1rem; display: ${isCollapsed ? 'none' : 'block'};">
                        
                        <!-- Missing Files Warning -->
                        ${!validation.isValid ? `
                            <div style="margin-bottom: 1rem; padding: 0.75rem; background: #fef2f2; border: 1px solid #fee2e2; border-radius: 0.5rem;">
                                <h5 style="margin: 0 0 0.5rem 0; color: var(--error-color); font-size: 0.875rem;">Missing Files:</h5>
                                <ul style="margin: 0; padding-left: 1.5rem; color: #b91c1c; font-size: 0.875rem;">
                                    ${validation.missingFiles.map(f => `<li>${f}</li>`).join('')}
                                </ul>
                            </div>
                        ` : ''}

                        <!-- Segments -->
            `;

            for (const [segment, files] of Object.entries(segments as any)) {
                html += `
                    <div style="margin-bottom: 1rem;">
                        <h4 style="margin: 0 0 0.5rem 0; font-size: 0.875rem; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.05em;">${segment}</h4>
                        <div style="display: flex; flex-direction: column; gap: 0.5rem;">
                `;
                
                (files as any[]).forEach((file: any) => {
                    html += `
                        <div style="padding: 0.5rem; background: var(--bg-color); border-radius: 0.25rem; font-size: 0.875rem; display: flex; align-items: center; justify-content: space-between;">
                            <span title="${escapeHtml(file.suffix)}">${escapeHtml(file.filename)}</span>
                            <button class="btn-icon-danger delete-file-btn" data-filename="${escapeHtml(file.filename)}" style="background:none; border:none; color: #ef4444; cursor: pointer; font-size: 1.2rem; line-height: 1;" title="Delete File">×</button>
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
                         if(arrow) arrow.textContent = isHidden ? '▲' : '▼';
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
                    workingFolder: currentCompetitionData.name,
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
                     loadCompetitionDetails(currentCompetitionData.name);
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
         openDeleteFileModal(filename, currentCompetitionData.name);
    };

    async function loadCompetitionDetails(name: string) {
        const container = document.getElementById('comp-files-container')!;
        container.innerHTML = '<p style="color: var(--text-secondary);">Scanning files...</p>';

        try {
            const resp = await fetch(`/api/get_competition_details?name=${encodeURIComponent(name)}`);
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
                 nameEl.style.color = '#ef4444'; // Red color
                 nameEl.style.fontWeight = '800';
            } else {
                 nameEl.style.color = ''; // Reset color
                 nameEl.style.fontWeight = '600';
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
                            <div style="margin-bottom: 0.5rem; display: flex; align-items: stretch; gap: 0.5rem;">
                                <a href="${safeUrl}" target="_blank" rel="noopener noreferrer" style="flex: 1; display: block; padding: 0.75rem; background: #f0f9ff; border: 1px solid #bae6fd; border-radius: 0.5rem; text-decoration: none; color: inherit; transition: background 0.2s;">
                                    <div style="display: flex; justify-content: space-between; align-items: center;">
                                        <span style="font-weight: 600; color: #0369a1; font-size: 0.9rem;">
                                            ${safeDescription}
                                            ${dateDisplay ? `<span style="color: #0c4a6e; font-size: 1rem; font-weight: 700; margin-left: 0.5rem;">${escapeHtml(dateDisplay)}</span>` : ''}
                                        </span>
                                        <div style="display: flex; gap: 0.5rem; align-items: center;">
                                            ${sizeStr ? `<span style="font-size: 0.75rem; color: #64748b; background: white; padding: 0.1rem 0.4rem; border-radius: 4px; border: 1px solid #e2e8f0;">${escapeHtml(sizeStr)}</span>` : ''}
                                            <span style="font-size: 0.75rem; color: #64748b; background: white; padding: 0.1rem 0.4rem; border-radius: 99px; border: 1px solid #e2e8f0;">Exp: ${escapeHtml(expStr)}</span>
                                        </div>
                                    </div>
                                    <div style="font-size: 0.8rem; color: #334155; margin-top: 0.25rem; display: flex; align-items: center;">
                                        ${safeFileName}
                                    </div>
                                </a>
                                <button class="btn-icon-copy copy-link-btn" data-url="${safeUrl}" style="background: white; border: 1px solid #bae6fd; border-radius: 0.5rem; color: #0369a1; width: 3rem; font-size: 1.2rem; cursor: pointer; display: flex; justify-content: center; align-items: center;" title="Copy Link to Clipboard">
                                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                                </button>
                                <button class="btn-icon-danger delete-gen-file-btn" data-filename="${safeJudgePapersPath}" style="background: white; border: 1px solid #fee2e2; border-radius: 0.5rem; color: #ef4444; width: 3rem; font-size: 1.5rem; cursor: pointer; display: flex; justify-content: center; align-items: center;" title="Delete File">
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
                                 el.style.color = 'var(--success-color)';
                                 el.style.borderColor = 'var(--success-color)';
                                 setTimeout(() => {
                                     el.innerHTML = originalHTML;
                                     el.style.color = '#0369a1';
                                     el.style.borderColor = '#bae6fd';
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
            container.innerHTML = '<p style="color: #ef4444;">Error loading files.</p>';
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
             optionsArea.innerHTML = '<p style="color: var(--text-secondary); font-size: 0.9rem;">No segments detected yet.</p>';
             return;
        }

        optionsArea.innerHTML = `
            <div style="margin-bottom: 0.75rem;">
                <h3 style="margin: 0; font-size: 0.85rem; color: var(--text-secondary); text-transform: uppercase; font-weight: 600;">Global Settings</h3>
            </div>

            <div style="margin-bottom: 0.75rem;">
                <label style="display: flex; align-items: center; cursor: pointer; gap: 0.5rem;">
                    <input type="checkbox" id="use-english-names" ${currentLanguage === 'en' ? 'checked' : ''} style="margin: 0;">
                    <span style="font-size: 0.85rem; color: var(--text-primary); line-height: 1.3;">Use English category names</span>
                </label>
            </div>
            
            <div style="margin-bottom: 0.75rem;">
                <label style="display: flex; align-items: center; cursor: pointer; gap: 0.5rem;">
                    <input type="checkbox" id="global-use-time-schedule" ${defaultUseTimeSchedule ? 'checked' : ''} style="margin: 0;">
                    <span style="font-size: 0.85rem; color: var(--text-primary); line-height: 1.3;">Use Time Schedule as a Segment cover page</span>
                </label>
            </div>

            <div style="border-top: 1px solid var(--border-color); padding-top: 0.75rem;">
                <button id="toggle-advanced-options" class="btn btn-ghost btn-xs" style="padding-left: 0; font-size: 0.8rem;">Show Per-Segment Settings ▸</button>
                <div id="segment-options-list" class="hidden" style="margin-top: 0.5rem; display: flex; flex-direction: column; gap: 0.5rem; max-height: 300px; overflow-y: auto;">
                    ${segments.map(s => `
                        <label style="display: flex; align-items: start; gap: 0.5rem; font-size: 0.8rem;">
                            <input type="checkbox" class="time-schedule-toggle" data-prefix="${s.prefix}" ${defaultUseTimeSchedule ? 'checked' : ''} style="margin-top: 0.15rem;">
                            <span style="line-height: 1.3;">${s.label}</span>
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
                            name: currentCompetitionData.name,
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

    async function handleFiles(files: FileList, competitionName: string) {
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
                const url = `/api/upload_file?competition=${encodeURIComponent(competitionName)}&filename=${encodeURIComponent(file.name)}`;
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
             loadCompetitionDetails(competitionName);
        }
    }

    async function openCompetition(name: string) {
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
            if (files && files.length > 0) await handleFiles(files, name);
            fileInput.value = ''; 
        };
        
        dropArea.ondragover = (e) => { e.preventDefault(); dropArea.classList.add('dragover'); };
        dropArea.ondragleave = (e) => { e.preventDefault(); dropArea.classList.remove('dragover'); };
        dropArea.ondrop = (e) => {
            e.preventDefault();
            dropArea.classList.remove('dragover');
            if (e.dataTransfer && e.dataTransfer.files.length > 0) {
                handleFiles(e.dataTransfer.files, name);
            }
        };

        // Initialize Options Area if not present
        let optionsArea = document.getElementById('options-area');
        // No longer dynamically creating, as it is in the static template now
        if (optionsArea) optionsArea.innerHTML = ''; // Clear old on load

        loadCompetitionDetails(name);
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
        | { type: 'COMPETITION', name: string }
        | { type: 'FILE', filename: string, competition: string };

    let pendingAction: DeleteAction | null = null;

    function openDeleteCompetitionModal(compName: string) {
        pendingAction = { type: 'COMPETITION', name: compName };
        modalTitle.textContent = `Delete Competition?`;
        modalMessage.innerHTML = `Are you sure you want to delete <strong>${escapeHtml(compName)}</strong>?<br>This action cannot be undone and will permanently remove all associated files.`;
        
        // Add Checkbox
        modalExtra.innerHTML = `
            <label style="display: flex; align-items: flex-start; gap: 0.5rem; font-size: 0.875rem; cursor: pointer;">
                <input type="checkbox" id="confirm-delete-checkbox" style="margin-top: 0.25rem;">
                <span style="color: var(--text-primary);">I understand that this action is permanent.</span>
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
                const resp = await fetch(`/api/delete_competition?name=${encodeURIComponent(pendingAction.name)}`);
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
                    if (currentCompetitionData) loadCompetitionDetails(currentCompetitionData.name);
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
                <div style="padding: 0.75rem 1rem; border-bottom: 1px solid var(--border-color); font-size: 0.75rem; color: var(--text-secondary);">
                    Signed in as <br> <strong style="color: var(--text-primary);">${escapeHtml(user.userDetails)}</strong>
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
