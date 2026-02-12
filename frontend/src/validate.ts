export type JudgingSystem = 'ISU';

export interface FileData {
    filename: string;
    suffix: string;
    type: string;
    category: string;
    categoryCode?: string;
    judgingMethod?: string; // "ISU" or "MUPI" — from categories table
    segment: string;
    raw_segment: string;
    prefix?: string;
    segment_display_name?: string;
}

export interface ValidationResult {
    isValid: boolean;
    missingFiles: string[];
}

export interface CompetitionValidationResult {
    isValid: boolean;
    missingFiles: string[];
}

const COMMON_FILES = [
    'StartListwithTimes.pdf',
    'ISUPanelofJudgesandTechnicalPanel.pdf',
    'JudgesSheetAll.pdf',
    'RefereeSheet.pdf'
];

const ISU_ONLY_FILES = [
    'PlannedProgramContent.pdf',
    'TechnicalControllerSheet.pdf',
    'TechnicalSpecialistSheet' // Special handling for regex/prefix
];

/**
 * Segments to skip during per-segment validation (these are meta-segments,
 * not actual skating segments that require the standard file set).
 */
const SKIP_SEGMENTS = ['Category General', 'General'];

/**
 * Validates competition-level requirements (files needed across all categories).
 *
 * CompetitionSchedule.pdf is optional — start times are extracted from
 * StartListwithTimes.pdf per segment, so the schedule is not required.
 *
 * @param competitionFiles - array of competition-wide file objects returned
 *        by the backend in the "competitionFiles" field.
 */
export function validateCompetition(
    _competitionFiles: FileData[]
): CompetitionValidationResult {
    // No required competition-level files at this time.
    return { isValid: true, missingFiles: [] };
}

export function validateCategory(
    segments: Record<string, FileData[]>
): ValidationResult {
    const missing: string[] = [];
    
    // 1. Check Category General files
    // My python logic: elif "CalculationSetupVerificationforReferee" in suffix: segment = "Category General"
    
    const allFiles = Object.values(segments).flat();
    const hasCalcSetup = allFiles.some(f => f.suffix === 'CalculationSetupVerificationforReferee.pdf');
    
    if (!hasCalcSetup) {
        missing.push('CalculationSetupVerificationforReferee.pdf (Category General)');
    }

    // 2. Check per-segment files
    // Skip meta-segments like 'Category General' and 'General'
    for (const [segmentName, files] of Object.entries(segments)) {
        if (SKIP_SEGMENTS.includes(segmentName)) {
            continue;
        }
        
        // Common files
        for (const req of COMMON_FILES) {
            const found = files.some(f => f.suffix === req);
            if (!found) {
                missing.push(`${req} (${segmentName})`);
            }
        }

        // ISU Specific Logic (Always applied for Figure Skating, skipped for MUPI)
        
        // Detect if this segment belongs to a MUPI category.
        // Uses the judgingMethod field from the backend (sourced from the categories table).
        const isMupi = files.some(f => f.judgingMethod === 'MUPI');

        if (!isMupi) {
            for (const req of ISU_ONLY_FILES) {
                if (req === 'TechnicalSpecialistSheet') {
                    // Check for at least one
                    // Starts with TechnicalSpecialistSheet because of the X (number)
                    const techCount = files.filter(f => f.suffix.startsWith('TechnicalSpecialistSheet')).length;
                    if (techCount < 1) {
                            missing.push(`TechnicalSpecialistSheet (At least one) (${segmentName})`);
                    }
                } else {
                    const found = files.some(f => f.suffix === req);
                    if (!found) {
                        missing.push(`${req} (${segmentName})`);
                    }
                }
            }
        }
    }

    return {
        isValid: missing.length === 0,
        missingFiles: missing
    };
}
