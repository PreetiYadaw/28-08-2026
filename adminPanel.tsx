// AdminPanel.tsx — Complete version with all features
// Changes vs previous version:
//   • application_name field added to User interface, NewUserData, editUser state
//   • Create/Edit user dialogs include Application Name selector (optional)
//   • RAG ingest includes Application Name selector (optional)
//   • RAG documents table shows Application column
//   • ExportUsersPanel — filter/sort/download all users as Excel
//   • ActivityDashboardPanel — date-range activity report with Excel export
//   • All panels integrated into AdminPanel as accordions
//   • APPLICATION_OPTIONS constant for app name dropdown

import React, { useState, useEffect } from 'react';
import {
    Box, Accordion, AccordionSummary, AccordionDetails, Typography, Button,
    TextField, Table, TableBody, TableCell, TableContainer, TableHead,
    TableRow, Paper, IconButton, Dialog, DialogTitle, DialogContent,
    DialogActions, Alert, CircularProgress, Chip, FormControl, InputLabel,
    Select, MenuItem, TablePagination, Tooltip, FormHelperText, Divider,
    Card, CardContent, LinearProgress,
} from '@mui/material';
import {
    ExpandMore, Edit, Lock, Visibility, VisibilityOff, PersonAdd,
    Refresh, Search, Assessment, Close, CloudUpload, Delete, AutoAwesome,
    Download, Person,
} from '@mui/icons-material';
import { SelectChangeEvent } from '@mui/material/Select';
import * as XLSX from 'xlsx';

// ============================================================================
// CONSTANTS
// ============================================================================

const API_BASE = 'http://localhost:1000/api/v1/testcase-generation';

const MAX_INGEST_MB = 70;

// Application options — update this list as needed
const APPLICATION_OPTIONS = [
'API Testing',
'APY',
'ATM',
'CBS',
'CKYC',
'CLEAR',
'CMP',
'CRH',
'CRM',
'DCR',
'DigiGov',
'E-PAY',
'FIGS',
'GBSS',
'GST',
'HRMS',
'LLMS Lite',
'NPS',
'Payment Systems',
'RLMS',
'SBI_Authenticator',
'SCFU',
'TradeFinance',
'UPI',
'Whatsapp Banking',
'WorkFlow',
'YONO 2.0',
'YONO Business'
];

// ── Department list ───────────────────────────────────────────────────────────
const DEPARTMENT_OPTIONS = [
    { value: '141', label: '141 - IT-Facility & Office Administration' },
    { value: '160', label: '160 - IT-Foreign Offices' },
    { value: '161', label: '161 - IT-Data Warehouse' },
    { value: '162', label: '162 - IT-Core Banking-Tech Operations' },
    { value: '164', label: '164 - IT-Core Banking-Operations' },
    { value: '166', label: '166 - IT-Development-Core Banking' },
    { value: '168', label: '168 - IT-Data Centre & Operations' },
    { value: '171', label: '171 - IT-Trade Finance & SCF' },
    { value: '172', label: '172 - IT-Payment System' },
    { value: '375', label: '375 - IT-Special Projects - Government' },
    { value: '377', label: '377 - IT-Partner Relationship' },
    { value: '378', label: '378 - IT-HRMS' },
    { value: '382', label: '382 - IT-Retail Loans' },
    { value: '385', label: '385 - IT-Complaints Management' },
    { value: '394', label: '394 - IT-Special Projects - Audit & Websites' },
    { value: '396', label: '396 - IT- Quality Assurance CoE' },
    { value: '398', label: '398 - IT-Platform Engineering - I' },
    { value: '401', label: '401 - IT-Enterprise & Technology Architecture' },
    { value: '402', label: '402 - IT Treasury Support And Services' },
    { value: '405', label: '405 - IT-ATM' },
    { value: '407', label: '407 - IT-UPI' },
    { value: '408', label: '408 - IT-Internet Banking' },
    { value: '409', label: '409 - IT-Business Intelligence & RFAA' },
    { value: '410', label: '410 - IT-Platform Engineering - II' },
    { value: '411', label: '411 - IT-Financial Inclusion & Government Schemes' },
    { value: '412', label: '412 - IT-Human Resources' },
    { value: '413', label: '413 - IT- Software Factory' },
    { value: '415', label: '415 - IT- Digital Channel Reconciliation Department' },
    { value: '416', label: '416 - IT- Operations and Settlement Department' },
    { value: '418', label: '418 - IT-Special Projects – Resources' },
    { value: '423', label: '423 - IT-YONO- Infra & Operations' },
    { value: '426', label: '426 - IT-CRM' },
    { value: '429', label: '429 - IT-E-Pay & Payment Gateway' },
    { value: '437', label: '437 - IT-Enterprise Integration Services' },
    { value: '439', label: '439 - IT-YONO DEVELOPMENT' },
    { value: '441', label: '441 - IT – FO Tech Ops' },
    { value: '446', label: '446 - IT-DMO' },
    { value: '448', label: '448 - IT-CMP' },
    { value: '450', label: '450 - IT-Corporate & SME Loans' },
    { value: '457', label: '457 - IT- Contact Centre Operations' },
    { value: '462', label: '462 - IT-Agri Tech' },
    { value: '465', label: '465 - IT-YONO 2.0 Development' },
    { value: '466', label: '466 - IT-Project Management Dept & Strategic Coordination' },
    { value: '467', label: '467 - IT-YONO Business' },
    { value: '468', label: '468 - IT-RRBs & SUBSIDIARIES' },
    { value: '470', label: '470 - IT-Governance' },
    { value: '475', label: '475 - IT-Regulatory Applications' },
    { value: '476', label: '476 - IT-Tech Operations Loans' },
    { value: '477', label: '477 - IT-YONO 2.0 Foundation Services & Infra' },
    { value: '478', label: '478 - IT-YONO 2.0 Ops' },
    { value: '479', label: '479 - IT-Network Operation' },
    { value: '480', label: '480 - IT-Network Technology' },
    { value: '481', label: '481 - IT-Core Banking-Tech Revamp' },
    { value: '482', label: '482 - IT Platform Engineering- III' },
    { value: '483', label: '483 - IT-ROC-Process & Control' },
    { value: '484', label: '484 - IT–Cloud Solutions' },
    { value: '485', label: '485 - IT-Software Factory Infra & OPS' },
    { value: '488', label: '488 - IT-JVs & Partnerships' },
];

// ============================================================================
// TYPES
// ============================================================================

interface User {
    id: number;
    first_name: string;
    last_name: string;
    username: string;
    email: string;
    departmentid?: string;
    role: string;
    is_active: number;
    disabled: boolean;
    created_at: string;
    updated_at: string;
    testcase_client: string;
    application_name?: string;   // NEW
}

interface NewUserData {
    first_name: string;
    last_name: string;
    username: string;
    email: string;
    password: string;
    departmentid: string;
    role: string;
    is_active: number;
    testcase_client: string;
    application_name: string;   // NEW
}

interface UserActivity {
    uuid: string;
    username: string;
    document_name: string;
    file_type: string;
    total_pages: number;
    testcase_client: string;
    generation_completed: boolean;
    total_pages_processed: number | null;
    created_at: string;
}

interface UserActivityResponse {
    username: string;
    user_info: {
        first_name: string;
        last_name: string;
        email: string;
        department_id: string;
    };
    summary_statistics: {
        total_activities: number;
        completed_activities: number;
        pending_activities: number;
        total_pages_processed: number;
        total_successful_generations: number;
        total_failed_generations: number;
        uat_generations: number;
        sit_generations: number;
        pdf_uploads: number;
        docx_uploads: number;
    };
    pagination: {
        returned_count: number;
        skip: number;
        limit: number;
        sort_order: string;
    };
    activities: UserActivity[];
}

interface RagDocument {
    doc_id: string;
    filename: string;
    total_chunks: number;
    department_id?: string;
    application_name?: string;   // NEW
}

// ============================================================================
// STYLING SHORTHANDS
// ============================================================================

const ragCard       = { border: '1px solid #1aa7d1', borderRadius: 3, boxShadow: '0 4px 14px rgba(26,167,209,0.12)' };
const dialogTitleSx = { bgcolor: '#F4FCFF', borderBottom: '2px solid #1aa7d1', fontWeight: 600 };
const titleWithBorder = { fontWeight: 600, borderLeft: '4px solid #1aa7d1', pl: 1 };
const primaryBtnSx  = { borderRadius: 2, textTransform: 'none', fontWeight: 600 };
const gradientBtnSx = { background: 'linear-gradient(135deg, #1aa7d1 0%, #1f3c88 100%)', textTransform: 'none', fontWeight: 600 };

// ============================================================================
// TEST ENVIRONMENT SELECTOR (reusable)
// ============================================================================

const TestEnvSelector: React.FC<{ value: string; onChange: (v: string) => void }> = ({ value, onChange }) => (
    <Box>
        <Typography variant="body2" sx={{ fontWeight: 600, mb: 0.5 }}>Test Environment</Typography>
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
            Controls which test case style is generated for this user (UAT or SIT).
        </Typography>
        <Box sx={{ display: 'flex', gap: 2 }}>
            {(['UAT', 'SIT'] as const).map(env => (
                <Box
                    key={env}
                    onClick={() => onChange(env)}
                    sx={{
                        flex: 1, p: 1.5, border: 2,
                        borderColor: value === env ? '#1aa7d1' : 'grey.300',
                        borderRadius: 2,
                        bgcolor: value === env ? '#F4FCFF' : 'transparent',
                        cursor: 'pointer', transition: 'all 0.2s',
                        '&:hover': { borderColor: '#1aa7d1', bgcolor: '#F4FCFF' },
                    }}
                >
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                        <Box sx={{
                            width: 16, height: 16, borderRadius: '50%', border: 2,
                            borderColor: value === env ? '#1aa7d1' : 'grey.400',
                            display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                        }}>
                            {value === env && <Box sx={{ width: 8, height: 8, borderRadius: '50%', bgcolor: '#1aa7d1' }} />}
                        </Box>
                        <Typography variant="body2" sx={{ fontWeight: value === env ? 700 : 500, color: value === env ? '#1aa7d1' : 'text.primary' }}>
                            {env}
                        </Typography>
                    </Box>
                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.25, ml: 3 }}>
                        {env === 'UAT' ? 'User Acceptance Testing' : 'System Integration Testing'}
                    </Typography>
                </Box>
            ))}
        </Box>
    </Box>
);

// ============================================================================
// APPLICATION SELECTOR (reusable)
// ============================================================================

const AppSelector: React.FC<{
    value: string;
    onChange: (v: string) => void;
    label?: string;
    helperText?: string;
}> = ({ value, onChange, label = 'Application (optional)', helperText }) => (
    <Box>
        <FormControl fullWidth>
            <InputLabel>{label}</InputLabel>
            <Select
                value={value}
                label={label}
                onChange={(e: SelectChangeEvent) => onChange(e.target.value)}
            >
                <MenuItem value=""><em>None / All</em></MenuItem>
                {APPLICATION_OPTIONS.map(a => <MenuItem key={a} value={a}>{a}</MenuItem>)}
            </Select>
        </FormControl>
        {helperText && (
            <FormHelperText sx={{ mt: 0.5 }}>{helperText}</FormHelperText>
        )}
    </Box>
);

// ============================================================================
// RAG KNOWLEDGE BASE PANEL
// ============================================================================

const RagKnowledgeBasePanel: React.FC = () => {
    const [documents, setDocuments]   = useState<RagDocument[]>([]);
    const [loading, setLoading]       = useState(false);
    const [uploading, setUploading]   = useState(false);
    const [deletingId, setDeletingId] = useState<string | null>(null);
    const [error, setError]           = useState('');
    const [success, setSuccess]       = useState('');
    const [ingestDept, setIngestDept] = useState('');
    const [ingestApp,  setIngestApp]  = useState('');   // NEW
    const [deptError,  setDeptError]  = useState('');
    const fileRef = React.useRef<HTMLInputElement>(null);
    const [ingestJob, setIngestJob]   = useState<{jobId: string; filename: string; status: string; error?: string} | null>(null);

    const fetchDocuments = async () => {
        setLoading(true); setError('');
        try {
            const res = await fetch(`${API_BASE}/rag-documents`, { credentials: 'include' });
            if (!res.ok) throw new Error('Failed to fetch RAG documents');
            setDocuments((await res.json()).documents || []);
        } catch (err: any) { setError(err.message); }
        finally { setLoading(false); }
    };

    useEffect(() => { fetchDocuments(); }, []);

    const handleIngest = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;

        if (!ingestDept) {
            setDeptError('Please select a department before ingesting.');
            if (fileRef.current) fileRef.current.value = '';
            return;
        }
        setDeptError('');

        if (!file.name.endsWith('.pdf')) { setError('Only PDF files can be ingested.'); return; }
        if (file.size > MAX_INGEST_MB * 1024 * 1024) {
            setError(`File too large (${(file.size / 1024 / 1024).toFixed(1)} MB). Max ${MAX_INGEST_MB} MB.`);
            return;
        }

        setUploading(true); setError(''); setSuccess('');
        try {
            const fd = new FormData();
            fd.append('file', file);
            fd.append('department_id', ingestDept);
            if (ingestApp) fd.append('application_name', ingestApp);
            const res = await fetch(`${API_BASE}/ingest-rag`, {
                method: 'POST', credentials: 'include', body: fd,
            });
            if (!res.ok) throw new Error((await res.json()).detail || 'Ingestion start failed');
            const data = await res.json();

            // Returns immediately with job_id
            setIngestJob({ jobId: data.job_id, filename: data.filename, status: 'running' });
            setSuccess(`⏳ Ingestion started for "${data.filename}" (Job ID: ${data.job_id}). You can continue using the app — we'll notify you when done.`);
            setIngestDept('');
            setIngestApp('');

            // Poll in background
            const poll = async () => {
                for (let i = 0; i < 300; i++) {  // max ~25 min polling
                    await new Promise(r => setTimeout(r, 5000));
                    try {
                        const statusRes = await fetch(`${API_BASE}/ingest-rag/status/${data.job_id}`, { credentials: 'include' });
                        const statusData = await statusRes.json();

                        if (statusData.status === 'done') {
                            setSuccess(`✅ "${data.filename}" ingested — ${statusData.result?.total_chunks || 0} chunks stored.`);
                            setIngestJob(null);
                            fetchDocuments();
                            break;
                        } else if (statusData.status === 'failed') {
                            setError(`✗ Ingestion failed for "${data.filename}": ${statusData.error}`);
                            setIngestJob(null);
                            break;
                        }
                        // still running — update status display
                        setIngestJob(prev => prev ? { ...prev, status: 'running' } : null);
                    } catch { /* ignore poll errors */ }
                }
            };
            poll();  // fire and forget

        } catch (err: any) { setError(err.message); }
        finally {
            setUploading(false);
            if (fileRef.current) fileRef.current.value = '';
        }
    };

    const handleDelete = async (docId: string, filename: string) => {
        if (!window.confirm(`Delete "${filename}" from the knowledge base?`)) return;
        setDeletingId(docId); setError(''); setSuccess('');
        try {
            const res = await fetch(`${API_BASE}/rag-documents/${docId}`, { method: 'DELETE', credentials: 'include' });
            if (!res.ok) throw new Error((await res.json()).detail || 'Delete failed');
            const data = await res.json();
            setSuccess(`✅ Deleted "${filename}" (${data.deleted_chunks} chunks removed)`);
            fetchDocuments();
        } catch (err: any) { setError(err.message); }
        finally { setDeletingId(null); }
    };

    const totalChunks = documents.reduce((s, d) => s + d.total_chunks, 0);

    // Group by department
    const docsByDept: Record<string, RagDocument[]> = {};
    documents.forEach(doc => {
        const dept = doc.department_id || 'general';
        if (!docsByDept[dept]) docsByDept[dept] = [];
        docsByDept[dept].push(doc);
    });

    return (
        <Box>
            <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 2 }}>
                <Box>
                    <Typography variant="h6" sx={{ fontWeight: 600 }}>RAG Knowledge Base</Typography>
                    <Typography variant="caption" color="text.secondary">
                        Documents ingested here are used as domain context during test case generation.
                        Each document is scoped to a department and optionally an application.
                    </Typography>
                </Box>
                <Button variant="outlined" startIcon={<Refresh />} onClick={fetchDocuments} disabled={loading} size="small">
                    Refresh
                </Button>
            </Box>

            {/* ── Ingest form ── */}
            <Box sx={{ border: '1px solid #1aa7d1', borderRadius: 2, p: 2, mb: 3, bgcolor: '#F4FCFF' }}>
                <Typography variant="body2" sx={{ fontWeight: 600, mb: 1.5, color: '#1f3c88' }}>
                    Ingest New Document
                </Typography>
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 2 }}>
                    Select a department (required) and optionally an application, then choose the PDF.
                </Typography>
                <Box sx={{ display: 'flex', gap: 2, alignItems: 'flex-start', flexWrap: 'wrap' }}>
                    {/* Department selector */}
                    <FormControl size="small" sx={{ minWidth: 260 }} error={!!deptError}>
                        <InputLabel>Department *</InputLabel>
                        <Select
                            value={ingestDept}
                            label="Department *"
                            onChange={(e: SelectChangeEvent) => { setIngestDept(e.target.value); setDeptError(''); }}
                        >
                            <MenuItem value=""><em>Select department</em></MenuItem>
                            {DEPARTMENT_OPTIONS.map(d => (
                                <MenuItem key={d.value} value={d.value}>{d.label}</MenuItem>
                            ))}
                        </Select>
                        {deptError && <FormHelperText>{deptError}</FormHelperText>}
                    </FormControl>

                    {/* Application selector — NEW */}
                    <FormControl size="small" sx={{ minWidth: 230 }}>
                        <InputLabel>Application (optional)</InputLabel>
                        <Select
                            value={ingestApp}
                            label="Application (optional)"
                            onChange={(e: SelectChangeEvent) => setIngestApp(e.target.value)}
                        >
                            <MenuItem value=""><em>None / All</em></MenuItem>
                            {APPLICATION_OPTIONS.map(a => (
                                <MenuItem key={a} value={a}>{a}</MenuItem>
                            ))}
                        </Select>
                    </FormControl>

                    {/* File input */}
                    <Box>
                        <input
                            accept=".pdf"
                            style={{ display: 'none' }}
                            id="rag-ingest-input"
                            type="file"
                            onChange={handleIngest}
                            ref={fileRef}
                            disabled={uploading}
                        />
                        <label htmlFor="rag-ingest-input">
                            <Button
                                variant="contained"
                                component="span"
                                startIcon={uploading ? <CircularProgress size={16} color="inherit" /> : <CloudUpload />}
                                disabled={uploading}
                                size="small"
                                sx={{ ...gradientBtnSx, height: 40 }}
                            >
                                {uploading ? 'Ingesting… (may take several minutes)' : 'Choose PDF & Ingest'}
                            </Button>
                        </label>
                        {ingestDept && (
                            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                                Dept: <strong>{DEPARTMENT_OPTIONS.find(d => d.value === ingestDept)?.label || ingestDept}</strong>
                                {ingestApp && <> · App: <strong>{ingestApp}</strong></>}
                            </Typography>
                        )}
                    </Box>
                </Box>
            </Box>

            {uploading && <LinearProgress sx={{ mb: 2, borderRadius: 1 }} />}
            {ingestJob && (
    <Alert severity="info" sx={{ mb: 2 }} icon={<CircularProgress size={18} />}>
        <Typography variant="body2">
            Ingestion in progress: "{ingestJob.filename}" — Job {ingestJob.jobId}.
            You can freely use other features. This panel will auto-refresh when done.
        </Typography>
    </Alert>
)}
            {error   && <Alert severity="error"   onClose={() => setError('')}   sx={{ mb: 2 }}>{error}</Alert>}
            {success && <Alert severity="success" onClose={() => setSuccess('')} sx={{ mb: 2 }}>{success}</Alert>}

            {/* Summary chips */}
            <Box sx={{ display: 'flex', gap: 2, mb: 3, flexWrap: 'wrap' }}>
                <Chip icon={<AutoAwesome />} label={`${documents.length} document${documents.length !== 1 ? 's' : ''}`} color="primary" variant="outlined" />
                <Chip label={`${totalChunks} total chunks`} variant="outlined" />
                <Chip label={`${Object.keys(docsByDept).length} department(s)`} variant="outlined" color="secondary" />
            </Box>

            {/* Document list */}
            {loading ? (
                <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress /></Box>
            ) : documents.length === 0 ? (
                <Alert severity="info">
                    No documents ingested yet. Select a department above and click <strong>Choose PDF &amp; Ingest</strong>.
                </Alert>
            ) : (
                Object.entries(docsByDept).map(([dept, deptDocs]) => (
                    <Box key={dept} sx={{ mb: 3 }}>
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
                            <Chip
                                label={`Dept: ${DEPARTMENT_OPTIONS.find(d => d.value === dept)?.label || dept}`}
                                size="small" color="primary" sx={{ fontWeight: 600, fontSize: '0.72rem' }}
                            />
                            <Typography variant="caption" color="text.secondary">{deptDocs.length} doc(s)</Typography>
                        </Box>
                        <TableContainer component={Paper} sx={{ border: '1px solid #1aa7d1', borderRadius: 2 }}>
                            <Table size="small">
                                <TableHead>
                                    <TableRow sx={{ bgcolor: '#22409A' }}>
                                        <TableCell sx={{ color: 'white', fontWeight: 600 }}>Filename</TableCell>
                                        <TableCell sx={{ color: 'white', fontWeight: 600 }}>Application</TableCell>
                                        <TableCell sx={{ color: 'white', fontWeight: 600 }} align="center">Chunks</TableCell>
                                        <TableCell sx={{ color: 'white', fontWeight: 600 }}>Doc ID</TableCell>
                                        <TableCell sx={{ color: 'white', fontWeight: 600 }} align="center">Delete</TableCell>
                                    </TableRow>
                                </TableHead>
                                <TableBody>
                                    {deptDocs.map(doc => (
                                        <TableRow key={doc.doc_id} hover>
                                            <TableCell>
                                                <Typography variant="body2" sx={{ fontWeight: 600 }}>{doc.filename}</Typography>
                                            </TableCell>
                                            <TableCell>
                                                <Chip
                                                    label={doc.application_name || '—'}
                                                    size="small"
                                                    variant="outlined"
                                                    sx={{ fontSize: '0.7rem' }}
                                                />
                                            </TableCell>
                                            <TableCell align="center">
                                                <Chip label={doc.total_chunks} size="small" color="primary" variant="outlined" />
                                            </TableCell>
                                            <TableCell>
                                                <Typography variant="caption" color="text.secondary" sx={{ fontFamily: 'monospace' }}>
                                                    {doc.doc_id.slice(0, 20)}…
                                                </Typography>
                                            </TableCell>
                                            <TableCell align="center">
                                                <Tooltip title="Delete from knowledge base">
                                                    <IconButton
                                                        size="small" color="error"
                                                        onClick={() => handleDelete(doc.doc_id, doc.filename)}
                                                        disabled={deletingId === doc.doc_id}
                                                    >
                                                        {deletingId === doc.doc_id ? <CircularProgress size={18} /> : <Delete fontSize="small" />}
                                                    </IconButton>
                                                </Tooltip>
                                            </TableCell>
                                        </TableRow>
                                    ))}
                                </TableBody>
                            </Table>
                        </TableContainer>
                    </Box>
                ))
            )}

            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 2 }}>
                💡 Only PDF files are supported. Max {MAX_INGEST_MB} MB.
                Users see documents for their department; if they have an application assigned,
                only documents for that application are shown.
            </Typography>
        </Box>
    );
};

// ============================================================================
// EXPORT USERS PANEL
// ============================================================================

const ExportUsersPanel: React.FC = () => {
    const [users, setUsers]               = useState<any[]>([]);
    const [loading, setLoading]           = useState(false);
    const [error, setError]               = useState('');
    const [deptFilter, setDeptFilter]     = useState('');
    const [roleFilter, setRoleFilter]     = useState('');
    const [activeFilter, setActiveFilter] = useState('');
    const [sortBy, setSortBy]             = useState('created_at');
    const [sortOrder, setSortOrder]       = useState('desc');
    const [page, setPage]                 = useState(0);
    const [rpp, setRpp]                   = useState(10);

    const fetchUsers = async () => {
        setLoading(true); setError('');
        try {
            const params = new URLSearchParams({ sort_by: sortBy, sort_order: sortOrder });
            if (deptFilter)        params.append('department', deptFilter);
            if (roleFilter)        params.append('role', roleFilter);
            if (activeFilter !== '') params.append('is_active', activeFilter);
            const res = await fetch(`${API_BASE}/admin/export-users?${params}`, { credentials: 'include' });
            if (!res.ok) throw new Error((await res.json()).detail || 'Failed to fetch users');
            setUsers((await res.json()).users);
        } catch (e: any) { setError(e.message); }
        finally { setLoading(false); }
    };

    useEffect(() => { fetchUsers(); }, [sortBy, sortOrder, deptFilter, roleFilter, activeFilter]);

    const downloadExcel = () => {
        if (!users.length) return;
        const exportCols = [
            'id', 'first_name', 'last_name', 'username', 'email',
            'departmentid', 'role', 'testcase_client', 'application_name',
            'is_active', 'login_count', 'created_at', 'updated_at',
        ];
        const rows = users.map(u => {
            const row: any = {};
            exportCols.forEach(c => { row[c] = u[c] ?? ''; });
            return row;
        });
        const ws = XLSX.utils.json_to_sheet(rows);
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, 'Users');
        ws['!cols'] = exportCols.map(c => ({ wch: Math.max(c.length, 18) }));
        XLSX.writeFile(wb, `users_export_${new Date().toISOString().slice(0, 10)}.xlsx`);
    };

    const COLS = ['Name', 'Username', 'Email', 'Dept', 'Role', 'Test Env', 'App', 'Status', 'Logins', 'Created'];

    return (
        <Box>
            {/* Header */}
            <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 2 }}>
                <Box>
                    <Typography variant="h6" sx={{ fontWeight: 600 }}>Export Users</Typography>
                    <Typography variant="caption" color="text.secondary">
                        Filter, sort and download all users as Excel
                    </Typography>
                </Box>
                <Box sx={{ display: 'flex', gap: 1 }}>
                    <Button variant="outlined" startIcon={<Refresh />} onClick={fetchUsers} disabled={loading} size="small">
                        Refresh
                    </Button>
                    <Button
                        variant="contained" startIcon={<Download />} onClick={downloadExcel}
                        disabled={!users.length} size="small" sx={gradientBtnSx}
                    >
                        Download Excel
                    </Button>
                </Box>
            </Box>

            {/* Filters */}
            <Box sx={{ display: 'flex', gap: 2, mb: 2, flexWrap: 'wrap' }}>
                <FormControl size="small" sx={{ minWidth: 220 }}>
                    <InputLabel>Department</InputLabel>
                    <Select value={deptFilter} label="Department"
                        onChange={(e: SelectChangeEvent) => { setDeptFilter(e.target.value); setPage(0); }}>
                        <MenuItem value=""><em>All Departments</em></MenuItem>
                        {DEPARTMENT_OPTIONS.map(d => <MenuItem key={d.value} value={d.value}>{d.label}</MenuItem>)}
                    </Select>
                </FormControl>
                <FormControl size="small" sx={{ minWidth: 120 }}>
                    <InputLabel>Role</InputLabel>
                    <Select value={roleFilter} label="Role"
                        onChange={(e: SelectChangeEvent) => { setRoleFilter(e.target.value); setPage(0); }}>
                        <MenuItem value=""><em>All</em></MenuItem>
                        <MenuItem value="user">User</MenuItem>
                        <MenuItem value="admin">Admin</MenuItem>
                    </Select>
                </FormControl>
                <FormControl size="small" sx={{ minWidth: 120 }}>
                    <InputLabel>Status</InputLabel>
                    <Select value={activeFilter} label="Status"
                        onChange={(e: SelectChangeEvent) => { setActiveFilter(e.target.value); setPage(0); }}>
                        <MenuItem value=""><em>All</em></MenuItem>
                        <MenuItem value="1">Active</MenuItem>
                        <MenuItem value="0">Inactive</MenuItem>
                    </Select>
                </FormControl>
                <FormControl size="small" sx={{ minWidth: 160 }}>
                    <InputLabel>Sort By</InputLabel>
                    <Select value={sortBy} label="Sort By" onChange={(e: SelectChangeEvent) => setSortBy(e.target.value)}>
                        <MenuItem value="created_at">Created Date</MenuItem>
                        <MenuItem value="username">Username</MenuItem>
                        <MenuItem value="first_name">First Name</MenuItem>
                        <MenuItem value="departmentid">Department</MenuItem>
                    </Select>
                </FormControl>
                <FormControl size="small" sx={{ minWidth: 130 }}>
                    <InputLabel>Order</InputLabel>
                    <Select value={sortOrder} label="Order" onChange={(e: SelectChangeEvent) => setSortOrder(e.target.value)}>
                        <MenuItem value="desc">Newest First</MenuItem>
                        <MenuItem value="asc">Oldest First</MenuItem>
                    </Select>
                </FormControl>
            </Box>

            <Chip label={`${users.length} user${users.length !== 1 ? 's' : ''}`} color="primary" variant="outlined" sx={{ mb: 2 }} />

            {error && <Alert severity="error" onClose={() => setError('')} sx={{ mb: 2 }}>{error}</Alert>}

            {loading ? (
                <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress /></Box>
            ) : (
                <>
                    <TableContainer component={Paper} sx={{ border: '1px solid #1aa7d1', borderRadius: 2 }}>
                        <Table size="small">
                            <TableHead>
                                <TableRow sx={{ bgcolor: '#22409A' }}>
                                    {COLS.map(h => (
                                        <TableCell key={h} sx={{ color: 'white', fontWeight: 600, fontSize: '0.78rem' }}>{h}</TableCell>
                                    ))}
                                </TableRow>
                            </TableHead>
                            <TableBody>
                                {users.slice(page * rpp, page * rpp + rpp).map(u => (
                                    <TableRow key={u.id} hover>
                                        <TableCell>
                                            <Typography variant="caption" sx={{ fontWeight: 600 }}>{u.first_name} {u.last_name}</Typography>
                                        </TableCell>
                                        <TableCell><Typography variant="caption">{u.username}</Typography></TableCell>
                                        <TableCell><Typography variant="caption">{u.email}</Typography></TableCell>
                                        <TableCell><Typography variant="caption">{u.departmentid || '—'}</Typography></TableCell>
                                        <TableCell>
                                            <Chip label={u.role?.toUpperCase()} size="small" color={u.role === 'admin' ? 'secondary' : 'default'} />
                                        </TableCell>
                                        <TableCell>
                                            <Chip
                                                label={u.testcase_client || 'UAT'} size="small" variant="outlined"
                                                color={(u.testcase_client || 'UAT') === 'SIT' ? 'secondary' : 'success'}
                                            />
                                        </TableCell>
                                        <TableCell>
                                            <Typography variant="caption">{u.application_name || '—'}</Typography>
                                        </TableCell>
                                        <TableCell>
                                            <Chip
                                                label={u.is_active === 1 ? 'Active' : 'Inactive'} size="small"
                                                color={u.is_active === 1 ? 'success' : 'error'}
                                            />
                                        </TableCell>
                                        <TableCell><Typography variant="caption">{u.login_count || 0}</Typography></TableCell>
                                        <TableCell>
                                            <Typography variant="caption">
                                                {u.created_at ? new Date(u.created_at).toLocaleDateString() : ''}
                                            </Typography>
                                        </TableCell>
                                    </TableRow>
                                ))}
                            </TableBody>
                        </Table>
                    </TableContainer>
                    <TablePagination
                        rowsPerPageOptions={[5, 10, 25, 50]}
                        component="div"
                        count={users.length}
                        rowsPerPage={rpp}
                        page={page}
                        onPageChange={(_, np) => setPage(np)}
                        onRowsPerPageChange={e => { setRpp(parseInt(e.target.value, 10)); setPage(0); }}
                    />
                </>
            )}
        </Box>
    );
};

// ============================================================================
// ACTIVITY DASHBOARD PANEL
// ============================================================================

const ActivityDashboardPanel: React.FC = () => {
    const todayStr = new Date().toISOString().slice(0, 10);
    const [startDate, setStartDate]   = useState(
        new Date(Date.now() - 30 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10)
    );
    const [endDate, setEndDate]       = useState(todayStr);
    const [todayOnly, setTodayOnly]   = useState(false);
    const [deptFilter, setDeptFilter] = useState('');
    const [tcFilter, setTcFilter]     = useState('');
    const [data, setData]             = useState<any>(null);
    const [loading, setLoading]       = useState(false);
    const [error, setError]           = useState('');
    const [actPage, setActPage]       = useState(0);
    const [actRpp, setActRpp]         = useState(10);

    const fetchData = async () => {
        setLoading(true); setError('');
        try {
            const params = new URLSearchParams();
            if (todayOnly) {
                params.append('today', 'true');
            } else {
                params.append('start_date', startDate);
                params.append('end_date', endDate);
            }
            if (deptFilter) params.append('department', deptFilter);
            if (tcFilter)   params.append('testcase_client', tcFilter);
            const res = await fetch(`${API_BASE}/admin/activity-dashboard?${params}`, { credentials: 'include' });
            if (!res.ok) throw new Error((await res.json()).detail || 'Failed to fetch activity data');
            setData(await res.json());
        } catch (e: any) { setError(e.message); }
        finally { setLoading(false); }
    };

    useEffect(() => { fetchData(); }, []);

    const downloadExcel = () => {
        if (!data?.activities?.length) return;
        const cols = [
            'username', 'department', 'application_name', 'document_name', 'file_type',
            'testcase_client', 'generation_completed', 'total_pages', 'total_pages_processed',
            'successful_generations', 'failed_generations', 'demand_id', 'project_id',
            'user_prompt_provided', 'created_at', 'generation_completed_at',
        ];
        const rows = data.activities.map((a: any) => {
            const r: any = {};
            cols.forEach(c => { r[c] = a[c] ?? ''; });
            return r;
        });
        const ws = XLSX.utils.json_to_sheet(rows);
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, 'Activities');
        ws['!cols'] = cols.map(c => ({ wch: Math.max(c.length, 16) }));
        const filename = todayOnly
            ? `activity_report_today_${todayStr}.xlsx`
            : `activity_report_${startDate}_to_${endDate}.xlsx`;
        XLSX.writeFile(wb, filename);
    };

    const StatCard: React.FC<{ label: string; value: number | string; color?: string }> = ({ label, value, color }) => (
        <Card sx={{ border: '1px solid #1aa7d1', borderRadius: 2, flex: 1, minWidth: 120 }}>
            <CardContent sx={{ p: 1.5, '&:last-child': { pb: 1.5 } }}>
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>{label}</Typography>
                <Typography variant="h5" sx={{ fontWeight: 700, color: color || '#1f3c88' }}>{value}</Typography>
            </CardContent>
        </Card>
    );

    return (
        <Box>
            {/* Header */}
            <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 2 }}>
                <Box>
                    <Typography variant="h6" sx={{ fontWeight: 600 }}>Activity Dashboard</Typography>
                    <Typography variant="caption" color="text.secondary">
                        Date-range activity report across all users
                    </Typography>
                </Box>
                <Box sx={{ display: 'flex', gap: 1 }}>
                    <Button variant="outlined" startIcon={<Refresh />} onClick={fetchData} disabled={loading} size="small">
                        Refresh
                    </Button>
                    <Button
                        variant="contained" startIcon={<Download />} onClick={downloadExcel}
                        disabled={!data?.activities?.length} size="small" sx={gradientBtnSx}
                    >
                        Download Excel
                    </Button>
                </Box>
            </Box>

            {/* Filter controls */}
            <Box sx={{ display: 'flex', gap: 2, mb: 2, flexWrap: 'wrap', alignItems: 'center' }}>
                {/* Today toggle */}
                <Box
                    onClick={() => setTodayOnly(!todayOnly)}
                    sx={{
                        border: 2, borderColor: todayOnly ? '#1aa7d1' : 'grey.300', borderRadius: 2,
                        px: 2, py: 0.75, cursor: 'pointer',
                        bgcolor: todayOnly ? '#F4FCFF' : 'transparent',
                        display: 'flex', alignItems: 'center', gap: 1,
                        transition: 'all 0.2s',
                        '&:hover': { borderColor: '#1aa7d1' },
                    }}
                >
                    <Box sx={{
                        width: 14, height: 14, borderRadius: '50%', border: 2,
                        borderColor: todayOnly ? '#1aa7d1' : 'grey.400',
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                    }}>
                        {todayOnly && <Box sx={{ width: 7, height: 7, borderRadius: '50%', bgcolor: '#1aa7d1' }} />}
                    </Box>
                    <Typography variant="body2" sx={{ fontWeight: 600 }}>Today Only</Typography>
                </Box>

                <TextField
                    size="small" label="Start Date" type="date" value={startDate}
                    onChange={e => setStartDate(e.target.value)} disabled={todayOnly}
                    InputLabelProps={{ shrink: true }} sx={{ width: 160 }}
                />
                <TextField
                    size="small" label="End Date" type="date" value={endDate}
                    onChange={e => setEndDate(e.target.value)} disabled={todayOnly}
                    InputLabelProps={{ shrink: true }} sx={{ width: 160 }}
                />
                <FormControl size="small" sx={{ minWidth: 220 }}>
                    <InputLabel>Department</InputLabel>
                    <Select value={deptFilter} label="Department"
                        onChange={(e: SelectChangeEvent) => setDeptFilter(e.target.value)}>
                        <MenuItem value=""><em>All Departments</em></MenuItem>
                        {DEPARTMENT_OPTIONS.map(d => <MenuItem key={d.value} value={d.value}>{d.label}</MenuItem>)}
                    </Select>
                </FormControl>
                <FormControl size="small" sx={{ minWidth: 120 }}>
                    <InputLabel>Test Env</InputLabel>
                    <Select value={tcFilter} label="Test Env"
                        onChange={(e: SelectChangeEvent) => setTcFilter(e.target.value)}>
                        <MenuItem value=""><em>All</em></MenuItem>
                        <MenuItem value="UAT">UAT</MenuItem>
                        <MenuItem value="SIT">SIT</MenuItem>
                    </Select>
                </FormControl>
                <Button
                    variant="contained" onClick={fetchData} disabled={loading} size="small"
                    sx={{ ...gradientBtnSx, height: 40, minWidth: 80 }}
                >
                    {loading ? <CircularProgress size={18} color="inherit" /> : 'Apply'}
                </Button>
            </Box>

            {error && <Alert severity="error" onClose={() => setError('')} sx={{ mb: 2 }}>{error}</Alert>}

            {loading && !data && (
                <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress /></Box>
            )}

            {data && (
                <>
                    {/* Date range info */}
                    <Alert severity="info" sx={{ mb: 2, py: 0.5 }}>
                        <Typography variant="caption">
                            Showing activities from <strong>{new Date(data.date_range.start).toLocaleDateString()}</strong> to <strong>{new Date(data.date_range.end).toLocaleDateString()}</strong>
                        </Typography>
                    </Alert>

                    {/* Summary stat cards */}
                    <Box sx={{ display: 'flex', gap: 1.5, mb: 3, flexWrap: 'wrap' }}>
                        <StatCard label="Total Activities"   value={data.summary.total_activities} />
                        <StatCard label="Completed"          value={data.summary.completed_activities}  color="#2e7d32" />
                        <StatCard label="Pending"            value={data.summary.pending_activities}     color="#e65100" />
                        <StatCard label="UAT"                value={data.summary.uat_generations}        color="#1565c0" />
                        <StatCard label="SIT"                value={data.summary.sit_generations}        color="#6a1b9a" />
                        <StatCard label="Pages Processed"    value={data.summary.total_pages_processed} />
                        <StatCard label="Successful Gen"     value={data.summary.total_successful_gen}   color="#2e7d32" />
                        <StatCard label="Failed Gen"         value={data.summary.total_failed_gen}       color="#c62828" />
                    </Box>

                    {/* Per-user summary table */}
                    {data.user_summary?.length > 0 && (
                        <Box sx={{ mb: 3 }}>
                            <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1 }}>
                                Per-User Summary ({data.user_summary.length} user{data.user_summary.length !== 1 ? 's' : ''})
                            </Typography>
                            <TableContainer component={Paper} sx={{ border: '1px solid #1aa7d1', borderRadius: 2 }}>
                                <Table size="small">
                                    <TableHead>
                                        <TableRow sx={{ bgcolor: '#22409A' }}>
                                            {['Username', 'Department', 'Total', 'Completed', 'UAT', 'SIT', 'Pages Processed'].map(h => (
                                                <TableCell key={h} sx={{ color: 'white', fontWeight: 600, fontSize: '0.78rem' }}>{h}</TableCell>
                                            ))}
                                        </TableRow>
                                    </TableHead>
                                    <TableBody>
                                        {data.user_summary.map((u: any) => (
                                            <TableRow key={u.username} hover>
                                                <TableCell>
                                                    <Typography variant="caption" sx={{ fontWeight: 600 }}>{u.username}</Typography>
                                                </TableCell>
                                                <TableCell><Typography variant="caption">{u.department || '—'}</Typography></TableCell>
                                                <TableCell><Chip label={u.total_activities} size="small" color="primary" variant="outlined" /></TableCell>
                                                <TableCell><Chip label={u.completed} size="small" color="success" variant="outlined" /></TableCell>
                                                <TableCell><Chip label={u.uat} size="small" color="info" variant="outlined" /></TableCell>
                                                <TableCell><Chip label={u.sit} size="small" color="secondary" variant="outlined" /></TableCell>
                                                <TableCell><Typography variant="caption">{u.pages_processed}</Typography></TableCell>
                                            </TableRow>
                                        ))}
                                    </TableBody>
                                </Table>
                            </TableContainer>
                        </Box>
                    )}

                    {/* Detailed activity log */}
                    <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1 }}>
                        Detailed Activity Log ({data.activities.length} record{data.activities.length !== 1 ? 's' : ''})
                    </Typography>
                    {data.activities.length === 0 ? (
                        <Alert severity="info">No activities found for the selected filters.</Alert>
                    ) : (
                        <>
                            <TableContainer component={Paper} sx={{ border: '1px solid #1aa7d1', borderRadius: 2 }}>
                                <Table size="small">
                                    <TableHead>
                                        <TableRow sx={{ bgcolor: '#22409A' }}>
                                            {['User', 'Dept', 'Document', 'File Type', 'Env', 'Status', 'Pages', 'Demand ID', 'Date'].map(h => (
                                                <TableCell key={h} sx={{ color: 'white', fontWeight: 600, fontSize: '0.75rem' }}>{h}</TableCell>
                                            ))}
                                        </TableRow>
                                    </TableHead>
                                    <TableBody>
                                        {data.activities.slice(actPage * actRpp, actPage * actRpp + actRpp).map((a: any) => (
                                            <TableRow key={a.id} hover>
                                                <TableCell>
                                                    <Typography variant="caption" sx={{ fontWeight: 600 }}>{a.username}</Typography>
                                                </TableCell>
                                                <TableCell><Typography variant="caption">{a.department || '—'}</Typography></TableCell>
                                                <TableCell sx={{ maxWidth: 200 }}>
                                                    <Tooltip title={a.document_name} arrow>
                                                        <Typography variant="caption" sx={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                            {a.document_name}
                                                        </Typography>
                                                    </Tooltip>
                                                </TableCell>
                                                <TableCell><Chip label={a.file_type?.toUpperCase()} size="small" /></TableCell>
                                                <TableCell>
                                                    <Chip
                                                        label={a.testcase_client} size="small" variant="outlined"
                                                        color={a.testcase_client === 'SIT' ? 'secondary' : 'success'}
                                                    />
                                                </TableCell>
                                                <TableCell>
                                                    <Chip
                                                        label={a.generation_completed ? 'Done' : 'Pending'} size="small"
                                                        color={a.generation_completed ? 'success' : 'warning'}
                                                    />
                                                </TableCell>
                                                <TableCell>
                                                    <Typography variant="caption">{a.total_pages_processed ?? '—'}</Typography>
                                                </TableCell>
                                                <TableCell>
                                                    <Typography variant="caption">{a.demand_id || '—'}</Typography>
                                                </TableCell>
                                                <TableCell>
                                                    <Typography variant="caption">
                                                        {a.created_at ? new Date(a.created_at).toLocaleString() : ''}
                                                    </Typography>
                                                </TableCell>
                                            </TableRow>
                                        ))}
                                    </TableBody>
                                </Table>
                            </TableContainer>
                            <TablePagination
                                rowsPerPageOptions={[5, 10, 25, 50]}
                                component="div"
                                count={data.activities.length}
                                rowsPerPage={actRpp}
                                page={actPage}
                                onPageChange={(_, np) => setActPage(np)}
                                onRowsPerPageChange={e => { setActRpp(parseInt(e.target.value, 10)); setActPage(0); }}
                            />
                        </>
                    )}
                </>
            )}
        </Box>
    );
};

// ============================================================================
// MAIN ADMIN PANEL
// ============================================================================

const AdminPanel: React.FC = () => {
    const [users, setUsers]         = useState<User[]>([]);
    const [loading, setLoading]     = useState(false);
    const [error, setError]         = useState('');
    const [success, setSuccess]     = useState('');
    const [page, setPage]           = useState(0);
    const [rowsPerPage, setRowsPerPage] = useState(10);

    // ── Dialogs ───────────────────────────────────────────────────────────────
    const [createOpen, setCreateOpen]     = useState(false);
    const [editOpen, setEditOpen]         = useState(false);
    const [pwdOpen, setPwdOpen]           = useState(false);
    const [detailsOpen, setDetailsOpen]   = useState(false);
    const [activityOpen, setActivityOpen] = useState(false);

    const [selectedUser, setSelectedUser]       = useState<User | null>(null);
    const [activityData, setActivityData]       = useState<UserActivityResponse | null>(null);
    const [activityLoading, setActivityLoading] = useState(false);
    const [activityPage, setActivityPage]       = useState(0);
    const [activityRpp, setActivityRpp]         = useState(10);

    const [searchUsername, setSearchUsername] = useState('');
    const [searchLoading, setSearchLoading]   = useState(false);

    // ── Create user form state ────────────────────────────────────────────────
    const [newUser, setNewUser] = useState<NewUserData>({
        first_name: '', last_name: '', username: '', email: '',
        password: '', departmentid: '', role: 'user', is_active: 1,
        testcase_client: 'UAT',
        application_name: '',   // NEW
    });

    // ── Edit user form state ──────────────────────────────────────────────────
    const [editUser, setEditUser] = useState({
        first_name: '', last_name: '', email: '', departmentid: '',
        role: 'user', is_active: 1, testcase_client: 'UAT',
        application_name: '',   // NEW
    });

    const [newPassword, setNewPassword] = useState('');
    const [showPwd, setShowPwd]         = useState(false);
    const [formErrors, setFormErrors]   = useState<Record<string, string>>({});

    // ── API calls ─────────────────────────────────────────────────────────────

    const fetchUsers = async () => {
        setLoading(true); setError('');
        try {
            const res = await fetch(`${API_BASE}/admin/users-list`, { credentials: 'include' });
            if (!res.ok) throw new Error('Failed to fetch users');
            setUsers((await res.json()).users);
        } catch (err: any) { setError(err.message); }
        finally { setLoading(false); }
    };

    useEffect(() => { fetchUsers(); }, []);

    const fetchUserByUsername = async (username: string) => {
        setSearchLoading(true); setError('');
        try {
            const res = await fetch(`${API_BASE}/admin/users/${username}`, { credentials: 'include' });
            if (!res.ok) throw new Error((await res.json()).detail || 'User not found');
            setSelectedUser(await res.json());
            setDetailsOpen(true);
        } catch (err: any) { setError(err.message); }
        finally { setSearchLoading(false); }
    };

    const fetchUserActivity = async (username: string) => {
        setActivityLoading(true); setError('');
        try {
            const res = await fetch(
                `${API_BASE}/user-activities/${username}?sort=descending&limit=100`,
                { credentials: 'include' }
            );
            if (!res.ok) throw new Error((await res.json()).detail || 'Failed to fetch activities');
            setActivityData(await res.json());
            setActivityOpen(true);
        } catch (err: any) { setError(err.message); }
        finally { setActivityLoading(false); }
    };

    // ── Validation ────────────────────────────────────────────────────────────

    const valEmail = (e: string) => {
        const ok = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(e) && e.toLowerCase().endsWith('@sbi.co.in');
        setFormErrors(p => ({ ...p, email: ok ? '' : !e.includes('@') ? 'Invalid email' : 'Must be @sbi.co.in' }));
        return ok;
    };
    const valUsername = (u: string) => {
        const ok = u.length >= 5 && /^[a-zA-Z0-9_]+$/.test(u);
        setFormErrors(p => ({ ...p, username: ok ? '' : u.length < 5 ? 'Min 5 chars' : 'Letters, numbers, underscores only' }));
        return ok;
    };
    const valPassword = (p: string) => {
        const ok = p.length >= 8 && /[A-Z]/.test(p) && /[a-z]/.test(p) && /[0-9]/.test(p) && /[@#$%^&*!]/.test(p);
        setFormErrors(v => ({
            ...v, password: ok ? '' :
                p.length < 8 ? 'Min 8 chars' : !/[A-Z]/.test(p) ? 'Need uppercase' :
                !/[a-z]/.test(p) ? 'Need lowercase' : !/[0-9]/.test(p) ? 'Need digit' : 'Need special char',
        }));
        return ok;
    };

    // ── CRUD handlers ─────────────────────────────────────────────────────────

    const handleCreateUser = async () => {
        setError(''); setSuccess('');
        if (!valEmail(newUser.email) || !valUsername(newUser.username) || !valPassword(newUser.password)) return;
        if (!newUser.first_name || !newUser.last_name || !newUser.departmentid) {
            setError('First name, last name, and department are required'); return;
        }
        setLoading(true);
        try {
            const payload = {
                ...newUser,
                application_name: newUser.application_name || null,
            };
            const res = await fetch(`${API_BASE}/admin/users`, {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (!res.ok) throw new Error((await res.json()).detail || 'Failed to create user');
            setSuccess('User created successfully');
            setCreateOpen(false);
            setNewUser({
                first_name: '', last_name: '', username: '', email: '',
                password: '', departmentid: '', role: 'user', is_active: 1,
                testcase_client: 'UAT', application_name: '',
            });
            setFormErrors({});
            fetchUsers();
        } catch (err: any) { setError(err.message); }
        finally { setLoading(false); }
    };

    const handleUpdateUser = async () => {
        if (!selectedUser) return;
        setError(''); setSuccess('');
        if (editUser.email && !valEmail(editUser.email)) return;
        setLoading(true);
        try {
            const payload = {
                ...editUser,
                application_name: editUser.application_name || null,
            };
            const res = await fetch(`${API_BASE}/admin/users/${selectedUser.username}`, {
                method: 'PATCH', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (!res.ok) throw new Error((await res.json()).detail || 'Failed to update user');
            setSuccess('User updated successfully');
            setEditOpen(false);
            setSelectedUser(null);
            fetchUsers();
        } catch (err: any) { setError(err.message); }
        finally { setLoading(false); }
    };

    const handleUpdatePassword = async () => {
        if (!selectedUser) return;
        setError(''); setSuccess('');
        if (!valPassword(newPassword)) return;
        setLoading(true);
        try {
            const res = await fetch(`${API_BASE}/admin/users/${selectedUser.username}/password`, {
                method: 'PATCH', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ new_password: newPassword }),
            });
            if (!res.ok) throw new Error((await res.json()).detail || 'Failed to update password');
            setSuccess('Password updated successfully');
            setPwdOpen(false);
            setSelectedUser(null);
            setNewPassword('');
            setFormErrors({});
        } catch (err: any) { setError(err.message); }
        finally { setLoading(false); }
    };

    const openEdit = (u: User) => {
        setSelectedUser(u);
        setEditUser({
            first_name      : u.first_name,
            last_name       : u.last_name,
            email           : u.email,
            departmentid    : u.departmentid || '',
            role            : u.role,
            is_active       : u.is_active,
            testcase_client : u.testcase_client || 'UAT',
            application_name: u.application_name || '',   // NEW
        });
        setEditOpen(true);
    };

    const openPwd = (u: User) => { setSelectedUser(u); setPwdOpen(true); };
    const formatDate = (d: string) => new Date(d).toLocaleString();

    // ── Render ────────────────────────────────────────────────────────────────

    return (
        <Box sx={{ p: 3, bgcolor: '#F4FCFF', minHeight: '100vh' }}>

            {/* ════════════════════════════════════════════════════════════════
                RAG KNOWLEDGE BASE
            ════════════════════════════════════════════════════════════════ */}
            <Card sx={{ ...ragCard, mb: 3 }}>
                <CardContent>
                    <Accordion defaultExpanded>
                        <AccordionSummary expandIcon={<ExpandMore />}>
                            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
                                <AutoAwesome sx={{ color: '#1aa7d1' }} />
                                <Box>
                                    <Typography variant="h6" sx={{ fontWeight: 600 }}>RAG Knowledge Base</Typography>
                                    <Typography variant="caption" color="text.secondary">
                                        Admin-only: manage reference documents for per-page test case enrichment
                                    </Typography>
                                </Box>
                            </Box>
                        </AccordionSummary>
                        <AccordionDetails><RagKnowledgeBasePanel /></AccordionDetails>
                    </Accordion>
                </CardContent>
            </Card>

            {/* ════════════════════════════════════════════════════════════════
                EXPORT USERS
            ════════════════════════════════════════════════════════════════ */}
            <Card sx={{ ...ragCard, mb: 3 }}>
                <CardContent>
                    <Accordion>
                        <AccordionSummary expandIcon={<ExpandMore />}>
                            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
                                <Download sx={{ color: '#1aa7d1' }} />
                                <Box>
                                    <Typography variant="h6" sx={{ fontWeight: 600 }}>Export Users</Typography>
                                    <Typography variant="caption" color="text.secondary">
                                        Filter, sort and download all users as Excel
                                    </Typography>
                                </Box>
                            </Box>
                        </AccordionSummary>
                        <AccordionDetails><ExportUsersPanel /></AccordionDetails>
                    </Accordion>
                </CardContent>
            </Card>

            {/* ════════════════════════════════════════════════════════════════
                ACTIVITY DASHBOARD
            ════════════════════════════════════════════════════════════════ */}
            <Card sx={{ ...ragCard, mb: 3 }}>
                <CardContent>
                    <Accordion>
                        <AccordionSummary expandIcon={<ExpandMore />}>
                            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
                                <Assessment sx={{ color: '#1aa7d1' }} />
                                <Box>
                                    <Typography variant="h6" sx={{ fontWeight: 600 }}>Activity Dashboard</Typography>
                                    <Typography variant="caption" color="text.secondary">
                                        Date-range activity report with per-user breakdown and Excel export
                                    </Typography>
                                </Box>
                            </Box>
                        </AccordionSummary>
                        <AccordionDetails><ActivityDashboardPanel /></AccordionDetails>
                    </Accordion>
                </CardContent>
            </Card>

            {/* ════════════════════════════════════════════════════════════════
                SEARCH USER
            ════════════════════════════════════════════════════════════════ */}
            <Card sx={{ ...ragCard, mb: 3 }}>
                <CardContent>
                    <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>Search User</Typography>
                    <Box sx={{ display: 'flex', gap: 2 }}>
                        <TextField
                            label="Username" value={searchUsername}
                            onChange={e => setSearchUsername(e.target.value)}
                            variant="outlined" size="small" sx={{ flex: 1 }}
                            onKeyPress={e => { if (e.key === 'Enter' && searchUsername) fetchUserByUsername(searchUsername); }}
                        />
                        <Button
                            sx={primaryBtnSx}
                            startIcon={searchLoading ? <CircularProgress size={20} /> : <Search />}
                            onClick={() => fetchUserByUsername(searchUsername)}
                            disabled={!searchUsername || searchLoading}
                        >
                            Search
                        </Button>
                        <Button
                            variant="outlined"
                            startIcon={activityLoading ? <CircularProgress size={20} /> : <Assessment />}
                            onClick={() => searchUsername && fetchUserActivity(searchUsername)}
                            disabled={!searchUsername || activityLoading}
                        >
                            View Activity
                        </Button>
                    </Box>
                </CardContent>
            </Card>

            {/* ════════════════════════════════════════════════════════════════
                ALL USERS TABLE
            ════════════════════════════════════════════════════════════════ */}
            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
                <Typography variant="h5" sx={{ fontWeight: 600 }}>All Users</Typography>
                <Box sx={{ display: 'flex', gap: 2 }}>
                    <Button variant="outlined" startIcon={<Refresh />} onClick={fetchUsers} disabled={loading}>
                        Refresh
                    </Button>
                    <Button sx={primaryBtnSx} startIcon={<PersonAdd />} onClick={() => setCreateOpen(true)}>
                        Create User
                    </Button>
                </Box>
            </Box>

            {error   && <Alert severity="error"   onClose={() => setError('')}   sx={{ mb: 2 }}>{error}</Alert>}
            {success && <Alert severity="success" onClose={() => setSuccess('')} sx={{ mb: 2 }}>{success}</Alert>}

            {loading && !users.length ? (
                <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress /></Box>
            ) : (
                <>
                    <TableContainer component={Paper} sx={{ mb: 2, border: '1px solid #1aa7d1', borderRadius: 3, boxShadow: '0 6px 18px rgba(26,167,209,0.12)' }}>
                        <Table>
                            <TableHead>
                                <TableRow sx={{ bgcolor: '#22409A' }}>
                                    {['Name', 'Username', 'Email', 'Department', 'Role', 'Test Env', 'Application', 'Status', 'Actions'].map(h => (
                                        <TableCell key={h} sx={{ color: 'white', fontWeight: 600 }}>{h}</TableCell>
                                    ))}
                                </TableRow>
                            </TableHead>
                            <TableBody>
                                {users.slice(page * rowsPerPage, page * rowsPerPage + rowsPerPage).map(u => (
                                    <TableRow key={u.id} hover>
                                        <TableCell>{u.first_name} {u.last_name}</TableCell>
                                        <TableCell>{u.username}</TableCell>
                                        <TableCell>{u.email}</TableCell>
                                        <TableCell>{u.departmentid || '—'}</TableCell>
                                        <TableCell>
                                            <Chip label={u.role.toUpperCase()} color={u.role === 'admin' ? 'secondary' : 'default'} size="small" />
                                        </TableCell>
                                        <TableCell>
                                            <Chip
                                                label={u.testcase_client || 'UAT'} size="small" variant="outlined"
                                                color={(u.testcase_client || 'UAT') === 'SIT' ? 'secondary' : 'success'}
                                                sx={{ fontWeight: 600 }}
                                            />
                                        </TableCell>
                                        <TableCell>
                                            <Typography variant="caption">{u.application_name || '—'}</Typography>
                                        </TableCell>
                                        <TableCell>
                                            <Chip
                                                label={u.is_active === 1 ? 'Active' : 'Inactive'}
                                                color={u.is_active === 1 ? 'success' : 'error'} size="small"
                                            />
                                        </TableCell>
                                        <TableCell>
                                            <Tooltip title="View Details">
                                                <IconButton size="small" color="info"
                                                    onClick={() => { setSelectedUser(u); setDetailsOpen(true); }}>
                                                    <Visibility fontSize="small" />
                                                </IconButton>
                                            </Tooltip>
                                            <Tooltip title="View Activity">
                                                <IconButton size="small" color="info"
                                                    onClick={() => fetchUserActivity(u.username)}>
                                                    <Assessment fontSize="small" />
                                                </IconButton>
                                            </Tooltip>
                                            <Tooltip title="Edit User">
                                                <IconButton size="small" color="primary" onClick={() => openEdit(u)}>
                                                    <Edit fontSize="small" />
                                                </IconButton>
                                            </Tooltip>
                                            <Tooltip title="Change Password">
                                                <IconButton size="small" color="primary" onClick={() => openPwd(u)}>
                                                    <Lock fontSize="small" />
                                                </IconButton>
                                            </Tooltip>
                                        </TableCell>
                                    </TableRow>
                                ))}
                            </TableBody>
                        </Table>
                    </TableContainer>
                    <TablePagination
                        rowsPerPageOptions={[5, 10, 25, 50]}
                        component="div"
                        count={users.length}
                        rowsPerPage={rowsPerPage}
                        page={page}
                        onPageChange={(_, np) => setPage(np)}
                        onRowsPerPageChange={e => { setRowsPerPage(parseInt(e.target.value, 10)); setPage(0); }}
                    />
                </>
            )}

            {/* ════════════════════════════════════════════════════════════════
                USER DETAILS DIALOG
            ════════════════════════════════════════════════════════════════ */}
            <Dialog open={detailsOpen} onClose={() => setDetailsOpen(false)} maxWidth="md" fullWidth>
                <DialogTitle sx={dialogTitleSx}>
                    <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <Typography variant="h6" sx={titleWithBorder}>User Details</Typography>
                        <IconButton onClick={() => setDetailsOpen(false)} size="small"><Close /></IconButton>
                    </Box>
                </DialogTitle>
                <DialogContent>
                    {selectedUser && (
                        <Box sx={{ mt: 2, display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' }, gap: 3 }}>
                            {([
                                ['First Name',       selectedUser.first_name],
                                ['Last Name',        selectedUser.last_name],
                                ['Username',         selectedUser.username],
                                ['Email',            selectedUser.email],
                                ['Department',       selectedUser.departmentid || 'Not specified'],
                                ['Application',      selectedUser.application_name || 'Not specified'],
                                ['Account Disabled', selectedUser.disabled ? 'Yes' : 'No'],
                                ['Created At',       formatDate(selectedUser.created_at)],
                                ['Last Updated',     formatDate(selectedUser.updated_at)],
                            ] as [string, string][]).map(([label, val]) => (
                                <Box key={label}>
                                    <Typography variant="subtitle2" color="text.secondary">{label}</Typography>
                                    <Typography variant="body1" sx={{ fontWeight: 600 }}>{val}</Typography>
                                </Box>
                            ))}
                            <Box>
                                <Typography variant="subtitle2" color="text.secondary">Role</Typography>
                                <Chip label={selectedUser.role.toUpperCase()} color={selectedUser.role === 'admin' ? 'secondary' : 'default'} size="small" />
                            </Box>
                            <Box>
                                <Typography variant="subtitle2" color="text.secondary">Test Environment</Typography>
                                <Chip
                                    label={selectedUser.testcase_client || 'UAT'} size="small" variant="outlined"
                                    color={(selectedUser.testcase_client || 'UAT') === 'SIT' ? 'secondary' : 'success'}
                                    sx={{ fontWeight: 600 }}
                                />
                            </Box>
                            <Box>
                                <Typography variant="subtitle2" color="text.secondary">Status</Typography>
                                <Chip
                                    label={selectedUser.is_active === 1 ? 'Active' : 'Inactive'}
                                    color={selectedUser.is_active === 1 ? 'success' : 'error'} size="small"
                                />
                            </Box>
                        </Box>
                    )}
                </DialogContent>
                <DialogActions>
                    <Button onClick={() => setDetailsOpen(false)}>Close</Button>
                </DialogActions>
            </Dialog>

            {/* ════════════════════════════════════════════════════════════════
                USER ACTIVITY DIALOG
            ════════════════════════════════════════════════════════════════ */}
            <Dialog open={activityOpen} onClose={() => setActivityOpen(false)} maxWidth="lg" fullWidth>
                <DialogTitle sx={dialogTitleSx}>
                    <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <Typography variant="h6" sx={titleWithBorder}>
                            User Activity: {activityData?.username}
                        </Typography>
                        <IconButton onClick={() => setActivityOpen(false)} size="small"><Close /></IconButton>
                    </Box>
                </DialogTitle>
                <DialogContent>
                    {activityLoading ? (
                        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress /></Box>
                    ) : activityData ? (
                        <Box sx={{ mt: 2 }}>
                            {/* User info card */}
                            <Card sx={{ ...ragCard, mb: 3 }}>
                                <CardContent>
                                    <Typography variant="h6" sx={{ mb: 2 }}>User Information</Typography>
                                    <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' }, gap: 2 }}>
                                        <Box>
                                            <Typography variant="body2" color="text.secondary">Name</Typography>
                                            <Typography variant="body1" sx={{ fontWeight: 600 }}>
                                                {activityData.user_info.first_name} {activityData.user_info.last_name}
                                            </Typography>
                                        </Box>
                                        <Box>
                                            <Typography variant="body2" color="text.secondary">Email</Typography>
                                            <Typography variant="body1" sx={{ fontWeight: 600 }}>{activityData.user_info.email}</Typography>
                                        </Box>
                                        <Box>
                                            <Typography variant="body2" color="text.secondary">Department</Typography>
                                            <Typography variant="body1" sx={{ fontWeight: 600 }}>
                                                {activityData.user_info.department_id || 'Not specified'}
                                            </Typography>
                                        </Box>
                                    </Box>
                                </CardContent>
                            </Card>

                            {/* Statistics */}
                            <Typography variant="h6" sx={{ mb: 2 }}>Activity Statistics</Typography>
                            <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr', md: 'repeat(4, 1fr)' }, gap: 2, mb: 3 }}>
                                {([
                                    ['Total Activities',  activityData.summary_statistics.total_activities,         ''],
                                    ['Completed',         activityData.summary_statistics.completed_activities,     'success.main'],
                                    ['Pending',           activityData.summary_statistics.pending_activities,       'warning.main'],
                                    ['Pages Processed',   activityData.summary_statistics.total_pages_processed,    ''],
                                    ['UAT Generations',   activityData.summary_statistics.uat_generations,          ''],
                                    ['SIT Generations',   activityData.summary_statistics.sit_generations,          ''],
                                    ['PDF Uploads',       activityData.summary_statistics.pdf_uploads,              ''],
                                    ['DOCX Uploads',      activityData.summary_statistics.docx_uploads,             ''],
                                ] as [string, number, string][]).map(([label, val, colour]) => (
                                    <Card key={label} sx={ragCard}>
                                        <CardContent>
                                            <Typography variant="body2" color="text.secondary">{label}</Typography>
                                            <Typography variant="h4" sx={{ fontWeight: 600, color: colour || 'inherit' }}>{val}</Typography>
                                        </CardContent>
                                    </Card>
                                ))}
                            </Box>

                            {/* Recent activity table */}
                            <Typography variant="h6" sx={{ mb: 2 }}>Recent Activities</Typography>
                            {activityData.activities.length === 0 ? (
                                <Alert severity="info">No activities found.</Alert>
                            ) : (
                                <>
                                    <TableContainer component={Paper}>
                                        <Table>
                                            <TableHead>
                                                <TableRow sx={{ bgcolor: '#22409A' }}>
                                                    {['Document', 'Type', 'Test Env', 'Pages', 'Status', 'Date'].map(h => (
                                                        <TableCell key={h} sx={{ color: 'white', fontWeight: 600 }}>{h}</TableCell>
                                                    ))}
                                                </TableRow>
                                            </TableHead>
                                            <TableBody>
                                                {activityData.activities
                                                    .slice(activityPage * activityRpp, activityPage * activityRpp + activityRpp)
                                                    .map(a => (
                                                        <TableRow key={a.uuid} hover>
                                                            <TableCell>{a.document_name}</TableCell>
                                                            <TableCell><Chip label={a.file_type.toUpperCase()} size="small" /></TableCell>
                                                            <TableCell>
                                                                <Chip
                                                                    label={a.testcase_client} size="small" variant="outlined"
                                                                    color={a.testcase_client === 'SIT' ? 'secondary' : 'success'}
                                                                    sx={{ fontWeight: 600 }}
                                                                />
                                                            </TableCell>
                                                            <TableCell>{a.total_pages_processed || a.total_pages || '—'}</TableCell>
                                                            <TableCell>
                                                                <Chip
                                                                    label={a.generation_completed ? 'Completed' : 'Pending'}
                                                                    color={a.generation_completed ? 'success' : 'warning'} size="small"
                                                                />
                                                            </TableCell>
                                                            <TableCell>{formatDate(a.created_at)}</TableCell>
                                                        </TableRow>
                                                    ))}
                                            </TableBody>
                                        </Table>
                                    </TableContainer>
                                    <TablePagination
                                        rowsPerPageOptions={[5, 10, 25, 50]}
                                        component="div"
                                        count={activityData.activities.length}
                                        rowsPerPage={activityRpp}
                                        page={activityPage}
                                        onPageChange={(_, np) => setActivityPage(np)}
                                        onRowsPerPageChange={e => { setActivityRpp(parseInt(e.target.value, 10)); setActivityPage(0); }}
                                    />
                                </>
                            )}
                        </Box>
                    ) : (
                        <Alert severity="info">No activity data available.</Alert>
                    )}
                </DialogContent>
                <DialogActions>
                    <Button onClick={() => setActivityOpen(false)}>Close</Button>
                </DialogActions>
            </Dialog>

            {/* ════════════════════════════════════════════════════════════════
                CREATE USER DIALOG
            ════════════════════════════════════════════════════════════════ */}
            <Dialog open={createOpen} onClose={() => setCreateOpen(false)} maxWidth="md" fullWidth>
                <DialogTitle sx={dialogTitleSx}>Create New User</DialogTitle>
                <DialogContent>
                    {/* Name row */}
                    <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 2, mt: 2 }}>
                        <TextField
                            label="First Name" value={newUser.first_name} required fullWidth
                            onChange={e => setNewUser({ ...newUser, first_name: e.target.value })}
                        />
                        <TextField
                            label="Last Name" value={newUser.last_name} required fullWidth
                            onChange={e => setNewUser({ ...newUser, last_name: e.target.value })}
                        />
                    </Box>
                    {/* Username */}
                    <TextField
                        label="Username" value={newUser.username} required fullWidth sx={{ mt: 2 }}
                        onChange={e => { setNewUser({ ...newUser, username: e.target.value }); valUsername(e.target.value); }}
                        error={!!formErrors.username} helperText={formErrors.username}
                    />
                    {/* Email */}
                    <TextField
                        label="Email (@sbi.co.in)" type="email" value={newUser.email} required fullWidth sx={{ mt: 2 }}
                        onChange={e => { setNewUser({ ...newUser, email: e.target.value }); valEmail(e.target.value); }}
                        error={!!formErrors.email} helperText={formErrors.email}
                    />
                    {/* Password */}
                    <TextField
                        label="Password" type={showPwd ? 'text' : 'password'} value={newUser.password} required fullWidth sx={{ mt: 2 }}
                        onChange={e => { setNewUser({ ...newUser, password: e.target.value }); valPassword(e.target.value); }}
                        error={!!formErrors.password} helperText={formErrors.password}
                        InputProps={{
                            endAdornment: (
                                <IconButton onClick={() => setShowPwd(!showPwd)} edge="end">
                                    {showPwd ? <VisibilityOff /> : <Visibility />}
                                </IconButton>
                            ),
                        }}
                    />
                    {/* Department */}
                    <FormControl fullWidth required sx={{ mt: 2 }}>
                        <InputLabel>Department</InputLabel>
                        <Select
                            value={newUser.departmentid} label="Department"
                            onChange={(e: SelectChangeEvent) => setNewUser({ ...newUser, departmentid: e.target.value })}
                        >
                            <MenuItem value=""><em>Select Department</em></MenuItem>
                            {DEPARTMENT_OPTIONS.map(d => <MenuItem key={d.value} value={d.value}>{d.label}</MenuItem>)}
                        </Select>
                    </FormControl>
                    {/* Role + Status row */}
                    <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 2, mt: 2 }}>
                        <FormControl fullWidth>
                            <InputLabel>Role</InputLabel>
                            <Select value={newUser.role} label="Role"
                                onChange={(e: SelectChangeEvent) => setNewUser({ ...newUser, role: e.target.value })}>
                                <MenuItem value="user">User</MenuItem>
                                <MenuItem value="admin">Admin</MenuItem>
                            </Select>
                        </FormControl>
                        <FormControl fullWidth>
                            <InputLabel>Status</InputLabel>
                            <Select value={String(newUser.is_active)} label="Status"
                                onChange={(e: SelectChangeEvent) => setNewUser({ ...newUser, is_active: parseInt(e.target.value) })}>
                                <MenuItem value="1">Active</MenuItem>
                                <MenuItem value="0">Inactive</MenuItem>
                            </Select>
                        </FormControl>
                    </Box>
                    {/* Test Environment */}
                    <Box sx={{ mt: 3 }}>
                        <TestEnvSelector
                            value={newUser.testcase_client}
                            onChange={v => setNewUser({ ...newUser, testcase_client: v })}
                        />
                    </Box>
                    {/* Application Name — NEW */}
                    <Box sx={{ mt: 3 }}>
                        <AppSelector
                            value={newUser.application_name}
                            onChange={v => setNewUser({ ...newUser, application_name: v })}
                            helperText="If set, this user will only see RAG documents tagged for this application. Leave empty to see all documents for their department."
                        />
                    </Box>
                </DialogContent>
                <DialogActions>
                    <Button onClick={() => setCreateOpen(false)}>Cancel</Button>
                    <Button onClick={handleCreateUser} sx={primaryBtnSx} disabled={loading}>
                        {loading ? <CircularProgress size={20} /> : 'Create User'}
                    </Button>
                </DialogActions>
            </Dialog>

            {/* ════════════════════════════════════════════════════════════════
                EDIT USER DIALOG
            ════════════════════════════════════════════════════════════════ */}
            <Dialog open={editOpen} onClose={() => setEditOpen(false)} maxWidth="md" fullWidth>
                <DialogTitle sx={dialogTitleSx}>Edit User: {selectedUser?.username}</DialogTitle>
                <DialogContent>
                    {/* Name row */}
                    <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 2, mt: 2 }}>
                        <TextField
                            label="First Name" value={editUser.first_name} fullWidth
                            onChange={e => setEditUser({ ...editUser, first_name: e.target.value })}
                        />
                        <TextField
                            label="Last Name" value={editUser.last_name} fullWidth
                            onChange={e => setEditUser({ ...editUser, last_name: e.target.value })}
                        />
                    </Box>
                    {/* Email */}
                    <TextField
                        label="Email" type="email" value={editUser.email} fullWidth sx={{ mt: 2 }}
                        onChange={e => { setEditUser({ ...editUser, email: e.target.value }); valEmail(e.target.value); }}
                        error={!!formErrors.email} helperText={formErrors.email}
                    />
                    {/* Department */}
                    <FormControl fullWidth sx={{ mt: 2 }}>
                        <InputLabel>Department</InputLabel>
                        <Select value={editUser.departmentid} label="Department"
                            onChange={(e: SelectChangeEvent) => setEditUser({ ...editUser, departmentid: e.target.value })}>
                            <MenuItem value=""><em>Select Department</em></MenuItem>
                            {DEPARTMENT_OPTIONS.map(d => <MenuItem key={d.value} value={d.value}>{d.label}</MenuItem>)}
                        </Select>
                    </FormControl>
                    {/* Role + Status row */}
                    <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 2, mt: 2 }}>
                        <FormControl fullWidth>
                            <InputLabel>Role</InputLabel>
                            <Select value={editUser.role} label="Role"
                                onChange={(e: SelectChangeEvent) => setEditUser({ ...editUser, role: e.target.value })}>
                                <MenuItem value="user">User</MenuItem>
                                <MenuItem value="admin">Admin</MenuItem>
                            </Select>
                        </FormControl>
                        <FormControl fullWidth>
                            <InputLabel>Status</InputLabel>
                            <Select value={String(editUser.is_active)} label="Status"
                                onChange={(e: SelectChangeEvent) => setEditUser({ ...editUser, is_active: parseInt(e.target.value) })}>
                                <MenuItem value="1">Active</MenuItem>
                                <MenuItem value="0">Inactive</MenuItem>
                            </Select>
                        </FormControl>
                    </Box>
                    {/* Test Environment */}
                    <Box sx={{ mt: 3 }}>
                        <TestEnvSelector
                            value={editUser.testcase_client}
                            onChange={v => setEditUser({ ...editUser, testcase_client: v })}
                        />
                    </Box>
                    {/* Application Name — NEW */}
                    <Box sx={{ mt: 3 }}>
                        <AppSelector
                            value={editUser.application_name}
                            onChange={v => setEditUser({ ...editUser, application_name: v })}
                            label="Application (optional)"
                            helperText="Select 'None / All' to clear — user will see all documents for their department."
                        />
                    </Box>
                </DialogContent>
                <DialogActions>
                    <Button onClick={() => setEditOpen(false)}>Cancel</Button>
                    <Button onClick={handleUpdateUser} sx={primaryBtnSx} disabled={loading}>
                        {loading ? <CircularProgress size={20} /> : 'Update User'}
                    </Button>
                </DialogActions>
            </Dialog>

            {/* ════════════════════════════════════════════════════════════════
                CHANGE PASSWORD DIALOG
            ════════════════════════════════════════════════════════════════ */}
            <Dialog open={pwdOpen} onClose={() => setPwdOpen(false)} maxWidth="sm" fullWidth>
                <DialogTitle sx={dialogTitleSx}>Update Password: {selectedUser?.username}</DialogTitle>
                <DialogContent>
                    <TextField
                        label="New Password" type={showPwd ? 'text' : 'password'} value={newPassword} fullWidth sx={{ mt: 2 }}
                        onChange={e => { setNewPassword(e.target.value); valPassword(e.target.value); }}
                        error={!!formErrors.password} helperText={formErrors.password}
                        InputProps={{
                            endAdornment: (
                                <IconButton onClick={() => setShowPwd(!showPwd)} edge="end">
                                    {showPwd ? <VisibilityOff /> : <Visibility />}
                                </IconButton>
                            ),
                        }}
                    />
                    <FormHelperText sx={{ mt: 1 }}>
                        Min 8 chars, 1 uppercase, 1 lowercase, 1 digit, 1 special char (@#$%^&*!)
                    </FormHelperText>
                </DialogContent>
                <DialogActions>
                    <Button onClick={() => setPwdOpen(false)}>Cancel</Button>
                    <Button onClick={handleUpdatePassword} sx={primaryBtnSx} disabled={loading}>
                        {loading ? <CircularProgress size={20} /> : 'Update Password'}
                    </Button>
                </DialogActions>
            </Dialog>

        </Box>
    );
};

// Avoid unused import warning — Search is used inline
const { HSearch } = { HSearch: require('@mui/icons-material/Search').default };

export default AdminPanel;
