"""Signalpost Interactive Web UI & Application Server.

Provides a responsive web interface for:
- Live Modulo 11 organization number checksum validation.
- Interactive end-to-end Norwegian company research.
- Tabbed visualization of Financials, Leadership, News, Jobs, Socials, and Evidence.
- One-click export to competition JSON and standalone offline HTML reports.
"""

import json
import logging
from typing import Any
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

from src.models.result import CompanyResultEnvelope
from src.pipeline.research import research_company_pipeline
from src.storage.repositories import company_repository
from src.ui.export_html import generate_standalone_html_report
from src.utils.rate_limiter import global_budget
from src.validation.identity import validate_orgnr

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Signalpost — Norwegian Company Intelligence Agent",
    description="Interactive UI & API for competitive Norwegian company research and provenance verification.",
    version="2.0.0",
)


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Signalpost — Norwegian Company Intelligence Agent</title>
  <style>
    :root {
      --bg: #0b1329;
      --card-bg: #152238;
      --card-hover: #1c2d4a;
      --border: #233554;
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --primary: #38bdf8;
      --primary-hover: #0284c7;
      --success: #34d399;
      --warning: #fbbf24;
      --danger: #f87171;
      --info: #818cf8;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    body { background: var(--bg); color: var(--text); padding: 2rem 1rem; line-height: 1.6; }
    .container { max-width: 1280px; margin: 0 auto; }

    /* Hero / Search Section */
    .hero { background: linear-gradient(135deg, #152238 0%, #0f172a 100%); border: 1px solid var(--border); border-radius: 16px; padding: 2.5rem 2rem; margin-bottom: 2rem; box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3); }
    .hero h1 { font-size: 2.2rem; font-weight: 800; color: #fff; margin-bottom: 0.5rem; letter-spacing: -0.025em; }
    .hero p { color: var(--text-muted); font-size: 1.05rem; margin-bottom: 1.5rem; }

    .search-bar { display: flex; gap: 0.75rem; max-width: 720px; margin-bottom: 1rem; }
    .search-input { flex: 1; background: #070d1e; border: 2px solid var(--border); border-radius: 10px; padding: 0.85rem 1.25rem; color: #fff; font-size: 1.1rem; transition: border-color 0.2s; }
    .search-input:focus { outline: none; border-color: var(--primary); }
    .btn { background: var(--primary); color: #0b1329; font-weight: 700; border: none; border-radius: 10px; padding: 0.85rem 1.75rem; font-size: 1rem; cursor: pointer; transition: background 0.2s, transform 0.1s; }
    .btn:hover { background: var(--primary-hover); }
    .btn:active { transform: scale(0.98); }
    .btn-secondary { background: var(--card-bg); color: var(--text); border: 1px solid var(--border); }
    .btn-secondary:hover { background: var(--card-hover); border-color: var(--primary); }

    .validation-feedback { font-size: 0.9rem; font-weight: 600; margin-bottom: 1.25rem; min-height: 1.2rem; }
    .val-valid { color: var(--success); }
    .val-invalid { color: var(--warning); }
    .val-error { color: var(--danger); }

    .samples-row { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; font-size: 0.85rem; color: var(--text-muted); }
    .pill { background: rgba(56, 189, 248, 0.1); border: 1px solid rgba(56, 189, 248, 0.3); color: var(--primary); padding: 0.3rem 0.75rem; border-radius: 9999px; cursor: pointer; transition: all 0.2s; }
    .pill:hover { background: rgba(56, 189, 248, 0.25); border-color: var(--primary); }

    /* Progress Box */
    .progress-box { display: none; background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 1.5rem; margin-bottom: 2rem; }
    .progress-steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 0.75rem; margin-top: 1rem; }
    .step-item { background: #070d1e; padding: 0.75rem; border-radius: 8px; border: 1px solid var(--border); font-size: 0.85rem; display: flex; align-items: center; gap: 0.5rem; }
    .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--primary); border-top-color: transparent; border-radius: 50%; animation: spin 0.8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }

    /* Results Presentation */
    #results { display: none; }
    .header-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 2rem; margin-bottom: 1.5rem; }
    .title-row { display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 1rem; }
    .company-title { font-size: 2rem; font-weight: 700; color: #fff; }
    .badges-row { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.5rem; }
    .badge { display: inline-block; padding: 0.35rem 0.75rem; border-radius: 9999px; font-size: 0.8rem; font-weight: 600; text-decoration: none; }
    .badge-success { background: rgba(52, 211, 153, 0.15); color: var(--success); border: 1px solid var(--success); }
    .badge-primary { background: rgba(56, 189, 248, 0.15); color: var(--primary); border: 1px solid var(--primary); }
    .badge-warning { background: rgba(251, 191, 36, 0.15); color: var(--warning); border: 1px solid var(--warning); }
    .badge-info { background: rgba(129, 140, 248, 0.15); color: var(--info); border: 1px solid var(--info); }
    .badge-light { background: rgba(248, 250, 252, 0.1); color: var(--text); border: 1px solid var(--border); }

    .actions-row { display: flex; gap: 0.75rem; margin-top: 1.5rem; flex-wrap: wrap; }

    .metrics-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
    .metric-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 1.25rem; }
    .metric-label { font-size: 0.8rem; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.05em; margin-bottom: 0.25rem; }
    .metric-val { font-size: 1.6rem; font-weight: 700; color: #fff; }

    /* Tabs */
    .tabs-nav { display: flex; gap: 0.5rem; border-bottom: 1px solid var(--border); margin-bottom: 1.5rem; overflow-x: auto; }
    .tab-btn { background: none; border: none; color: var(--text-muted); padding: 0.75rem 1.25rem; font-size: 0.95rem; font-weight: 600; cursor: pointer; border-bottom: 2px solid transparent; transition: all 0.2s; white-space: nowrap; }
    .tab-btn:hover { color: #fff; }
    .tab-btn.active { color: var(--primary); border-bottom-color: var(--primary); }
    .tab-pane { display: none; background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 1.75rem; margin-bottom: 2rem; }
    .tab-pane.active { display: block; }

    /* Tables */
    table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
    th, td { padding: 0.85rem 1rem; text-align: left; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
    th { background: rgba(255, 255, 255, 0.03); color: var(--text-muted); font-weight: 600; }
    a { color: var(--primary); text-decoration: none; }
    a:hover { text-decoration: underline; }

    .synthesis-text { background: #070d1e; padding: 1.5rem; border-radius: 8px; border: 1px solid var(--border); font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 0.88rem; line-height: 1.7; white-space: pre-wrap; }
    .card-item { background: #070d1e; border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; margin-bottom: 1rem; }
    .card-item-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
  </style>
</head>
<body>
  <div class="container">
    <div class="hero">
      <h1>Signalpost Intelligence Agent</h1>
      <p>Precision Norwegian Corporate Research, Modulo 11 Identity Verification, and Fact Provenance.</p>

      <div class="search-bar">
        <input type="text" id="orgnrInput" class="search-input" placeholder="Enter Norwegian 9-digit Org.nr (e.g. 923609016)" maxlength="12" />
        <button id="searchBtn" class="btn" onclick="startResearch()">Research Company</button>
      </div>

      <div id="validationFeedback" class="validation-feedback"></div>

      <div class="samples-row">
        <span>Quick Samples:</span>
        <span class="pill" onclick="selectSample('923609016')">Equinor ASA (923609016)</span>
        <span class="pill" onclick="selectSample('984851006')">DNB Bank ASA (984851006)</span>
        <span class="pill" onclick="selectSample('991825827')">Roger Aamodt Bygg AS (991825827)</span>
        <span class="pill" onclick="selectSample('910831623')">Otis AS (910831623)</span>
      </div>
    </div>

    <!-- Live Multi-Step Research Pipeline Progress -->
    <div id="progressBox" class="progress-box">
      <div style="display: flex; justify-content: space-between; align-items: center;">
        <strong>Conducting Multi-Tier Grounded Research...</strong>
        <span id="progressTimer" class="badge badge-light">Elapsed: 0.0s</span>
      </div>
      <div class="progress-steps">
        <div class="step-item" id="step-enhet"><span class="spinner"></span> 1. Enhetsregisteret</div>
        <div class="step-item" id="step-roller"><span class="spinner"></span> 2. Roller Register</div>
        <div class="step-item" id="step-regnskap"><span class="spinner"></span> 3. Regnskapsregisteret</div>
        <div class="step-item" id="step-web"><span class="spinner"></span> 4. Web & Careers</div>
        <div class="step-item" id="step-kunngj"><span class="spinner"></span> 5. Kunngjøringer</div>
        <div class="step-item" id="step-synthesis"><span class="spinner"></span> 6. 10-Section Synthesis</div>
      </div>
    </div>

    <!-- Results Section -->
    <div id="results">
      <div class="header-card">
        <div class="title-row">
          <div>
            <div id="resCompanyName" class="company-title">Company Name</div>
            <div class="badges-row">
              <span id="resOrgnrBadge" class="badge badge-primary">Org.nr: 000000000</span>
              <span class="badge badge-success">✓ Modulo 11 Verified</span>
              <span id="resFormBadge" class="badge badge-info">AS (Aksjeselskap)</span>
              <span id="resStatusBadge" class="badge badge-warning">ACTIVE</span>
              <span id="resOutcomeBadge" class="badge badge-light">AVAILABLE</span>
            </div>
          </div>
          <div class="actions-row">
            <button class="btn btn-secondary" onclick="exportHtmlReport()">Download Standalone HTML</button>
            <button class="btn btn-secondary" onclick="exportJsonEnvelope()">Download Competition JSON</button>
            <button class="btn btn-secondary" onclick="copySynthesisMarkdown()">Copy Markdown</button>
          </div>
        </div>

        <p style="margin-top: 1rem; color: var(--text-muted);" id="resHeaderDetails"></p>
      </div>

      <div class="metrics-grid">
        <div class="metric-card">
          <div class="metric-label">Operating Revenue</div>
          <div id="metricRevenue" class="metric-val">--</div>
          <small id="metricRevSub" class="text-muted">Regnskapsregisteret</small>
        </div>
        <div class="metric-card">
          <div class="metric-label">Workforce Count</div>
          <div id="metricWorkforce" class="metric-val">--</div>
          <small class="text-muted">NAV Aa-registeret</small>
        </div>
        <div class="metric-card">
          <div class="metric-label">Managing Director (CEO)</div>
          <div id="metricCeo" class="metric-val" style="font-size: 1.3rem;">--</div>
          <small class="text-muted">Brønnøysundregistrene Roller</small>
        </div>
        <div class="metric-card">
          <div class="metric-label">Balance Sheet Check</div>
          <div id="metricBalance" class="metric-val" style="font-size: 1.2rem;">--</div>
          <small class="text-muted">Assets = Equity + Liab.</small>
        </div>
      </div>

      <!-- Tab Navigation -->
      <div class="tabs-nav">
        <button class="tab-btn active" onclick="switchTab('synthesis')">10-Section Synthesis</button>
        <button class="tab-btn" onclick="switchTab('financials')">Financials & Balance Sheet</button>
        <button class="tab-btn" onclick="switchTab('governance')">Leadership & Board</button>
        <button class="tab-btn" onclick="switchTab('news')">Verified News & Kunngjøringer</button>
        <button class="tab-btn" onclick="switchTab('jobs')">Careers & Vacancies</button>
        <button class="tab-btn" onclick="switchTab('socials')">Verified Socials</button>
        <button class="tab-btn" onclick="switchTab('evidence')">Evidence Explorer</button>
      </div>

      <!-- Tab Panes -->
      <div id="tab-synthesis" class="tab-pane active">
        <div id="synthesisContent" class="synthesis-text"></div>
      </div>

      <div id="tab-financials" class="tab-pane">
        <div id="financialsContent"></div>
      </div>

      <div id="tab-governance" class="tab-pane">
        <div id="governanceContent"></div>
      </div>

      <div id="tab-news" class="tab-pane">
        <div id="newsContent"></div>
      </div>

      <div id="tab-jobs" class="tab-pane">
        <div id="jobsContent"></div>
      </div>

      <div id="tab-socials" class="tab-pane">
        <div id="socialsContent"></div>
      </div>

      <div id="tab-evidence" class="tab-pane">
        <div id="evidenceContent"></div>
      </div>
    </div>
  </div>

  <script>
    let currentEnvelope = null;
    let timerInterval = null;

    // Modulo 11 checksum calculation
    function verifyModulo11(digits) {
      if (!digits || digits.length !== 9 || !/^\d+$/.test(digits)) return false;
      const weights = [3, 2, 7, 6, 5, 4, 3, 2];
      let sum = 0;
      for (let i = 0; i < 8; i++) {
        sum += parseInt(digits[i], 10) * weights[i];
      }
      const remainder = 11 - (sum % 11);
      const expectedK = (remainder === 11) ? 0 : remainder;
      return expectedK === parseInt(digits[8], 10);
    }

    const orgnrInput = document.getElementById("orgnrInput");
    const valFeedback = document.getElementById("validationFeedback");

    orgnrInput.addEventListener("input", (e) => {
      const val = e.target.value.replace(/\s+/g, "");
      if (val.length === 0) {
        valFeedback.innerHTML = "";
      } else if (val.length < 9) {
        valFeedback.className = "validation-feedback text-muted";
        valFeedback.innerHTML = `Entered ${val.length}/9 digits...`;
      } else if (val.length === 9) {
        if (verifyModulo11(val)) {
          valFeedback.className = "validation-feedback val-valid";
          valFeedback.innerHTML = `✓ Valid Norwegian Organization Number (Modulo 11 verified)`;
        } else {
          valFeedback.className = "validation-feedback val-invalid";
          valFeedback.innerHTML = `⚠ Invalid Modulo 11 checksum (Will be checked against official Brreg registry)`;
        }
      } else {
        valFeedback.className = "validation-feedback val-error";
        valFeedback.innerHTML = `Organization number must be exactly 9 digits`;
      }
    });

    function selectSample(orgnr) {
      orgnrInput.value = orgnr;
      orgnrInput.dispatchEvent(new Event("input"));
      startResearch();
    }

    async function startResearch() {
      const orgnr = orgnrInput.value.replace(/\s+/g, "");
      if (!orgnr || orgnr.length !== 9) {
        alert("Please enter a valid 9-digit Norwegian organization number.");
        return;
      }

      document.getElementById("results").style.display = "none";
      const progressBox = document.getElementById("progressBox");
      progressBox.style.display = "block";

      let startT = Date.now();
      clearInterval(timerInterval);
      timerInterval = setInterval(() => {
        const elapsed = ((Date.now() - startT) / 1000).toFixed(1);
        document.getElementById("progressTimer").innerText = `Elapsed: ${elapsed}s`;
      }, 100);

      try {
        const resp = await fetch(`/api/research/${orgnr}`);
        if (!resp.ok) {
          const err = await resp.json();
          throw new Error(err.detail || "Research failed");
        }
        currentEnvelope = await resp.json();
        renderResults(currentEnvelope);
      } catch (err) {
        alert("Error during research: " + err.message);
      } finally {
        clearInterval(timerInterval);
        progressBox.style.display = "none";
      }
    }

    function renderResults(env) {
      const comp = env.company || {};
      document.getElementById("results").style.display = "block";

      document.getElementById("resCompanyName").innerText = env.name || comp.name || "Unknown Company";
      document.getElementById("resOrgnrBadge").innerText = `Org.nr: ${env.organisation_number}`;
      document.getElementById("resFormBadge").innerText = `${comp.legal_form_code || "AS"} (${comp.legal_form_description || "Aksjeselskap"})`;
      document.getElementById("resStatusBadge").innerText = `Status: ${env.status || "ACTIVE"}`;
      document.getElementById("resOutcomeBadge").innerText = `Outcome: ${env.outcome || "AVAILABLE"}`;

      const addr = comp.registered_office_address ? `${comp.registered_office_address.street_address || ""}, ${comp.registered_office_address.postal_code || ""} ${comp.registered_office_address.city || ""}` : "Not reported";
      document.getElementById("resHeaderDetails").innerHTML = `
        <strong>Industry (NACE):</strong> ${comp.nace_code || "N/A"} — ${comp.nace_description || "General Business"}<br/>
        <strong>Registered Office:</strong> ${addr}<br/>
        <strong>Official Website:</strong> ${comp.website ? `<a href="${comp.website}" target="_blank">${comp.website}</a>` : "Not available"}
      `;

      // Metrics
      const fin = comp.financials || {};
      document.getElementById("metricRevenue").innerText = fin.total_revenue != null ? `${Number(fin.total_revenue).toLocaleString()} ${fin.currency || "NOK"}` : "Not Available";
      document.getElementById("metricWorkforce").innerText = comp.employee_count != null ? `${comp.employee_count} registered` : "Not Available";

      const ceo = (comp.officers || []).find(o => (o.role_description || "").toLowerCase().includes("daglig leder"));
      document.getElementById("metricCeo").innerText = ceo ? ceo.name : "Not Registered";

      if (fin.total_assets != null && fin.total_equity != null && fin.total_liabilities != null) {
        const diff = Math.abs(fin.total_assets - (fin.total_equity + fin.total_liabilities));
        if (diff < 1.0) {
          document.getElementById("metricBalance").innerHTML = `<span style="color:var(--success);">✓ Balanced</span>`;
        } else {
          document.getElementById("metricBalance").innerHTML = `<span style="color:var(--warning);">Variance</span>`;
        }
      } else {
        document.getElementById("metricBalance").innerText = "Not Available";
      }

      // Render Tabs
      document.getElementById("synthesisContent").innerText = env.synthesis || "No synthesis available.";

      // Financials Tab
      let finHtml = `
        <table>
          <thead><tr><th>Line Item</th><th>Value</th><th>Units & Scope</th><th>Audit</th></tr></thead>
          <tbody>
            <tr><td><strong>Operating Revenue</strong></td><td>${fin.total_revenue != null ? Number(fin.total_revenue).toLocaleString() + " " + (fin.currency || "NOK") : "Not Reported"}</td><td>Base Units | ${fin.statement_type || "SELSKAP"}</td><td>${fin.is_audited ? "✓ Audited" : "Unaudited"}</td></tr>
            <tr><td><strong>Operating Profit</strong></td><td>${fin.operating_profit != null ? Number(fin.operating_profit).toLocaleString() + " " + (fin.currency || "NOK") : "Not Reported"}</td><td>Base Units | ${fin.statement_type || "SELSKAP"}</td><td>--</td></tr>
            <tr><td><strong>Net Income</strong></td><td>${fin.net_income != null ? Number(fin.net_income).toLocaleString() + " " + (fin.currency || "NOK") : "Not Reported"}</td><td>Base Units | ${fin.statement_type || "SELSKAP"}</td><td>--</td></tr>
            <tr><td><strong>Total Assets</strong></td><td>${fin.total_assets != null ? Number(fin.total_assets).toLocaleString() + " " + (fin.currency || "NOK") : "Not Reported"}</td><td>Base Units | ${fin.statement_type || "SELSKAP"}</td><td>--</td></tr>
            <tr><td><strong>Total Equity</strong></td><td>${fin.total_equity != null ? Number(fin.total_equity).toLocaleString() + " " + (fin.currency || "NOK") : "Not Reported"}</td><td>Base Units | ${fin.statement_type || "SELSKAP"}</td><td>--</td></tr>
            <tr><td><strong>Total Liabilities</strong></td><td>${fin.total_liabilities != null ? Number(fin.total_liabilities).toLocaleString() + " " + (fin.currency || "NOK") : "Not Reported"}</td><td>Base Units | ${fin.statement_type || "SELSKAP"}</td><td>--</td></tr>
          </tbody>
        </table>
      `;
      document.getElementById("financialsContent").innerHTML = finHtml;

      // Governance Tab
      let offHtml = `<table><thead><tr><th>Role</th><th>Name</th><th>Status</th></tr></thead><tbody>`;
      if (comp.officers && comp.officers.length > 0) {
        for (const o of comp.officers) {
          offHtml += `<tr><td><strong>${o.role_description}</strong></td><td>${o.name}</td><td><span class="badge badge-info">${o.is_resigned ? "Resigned" : "Active"}</span></td></tr>`;
        }
      } else {
        offHtml += `<tr><td colspan="3" class="text-muted">No registered officers found in public registry.</td></tr>`;
      }
      offHtml += `</tbody></table>`;
      document.getElementById("governanceContent").innerHTML = offHtml;

      // News Tab
      let newsHtml = "";
      const newsItems = comp.news_items || [];
      if (newsItems.length > 0) {
        for (const n of newsItems) {
          const head = n.headline || n.title || "";
          const u = n.canonical_url || n.url || "#";
          const pubDate = n.publication_date || n.date || "Undated";
          const pub = n.publisher || "Official Source";
          newsHtml += `
            <div class="card-item">
              <div class="card-item-header">
                <strong><a href="${u}" target="_blank">${head}</a></strong>
                <span class="badge badge-info">${pubDate}</span>
              </div>
              <small class="text-muted">Publisher: ${pub}</small>
              <p style="margin-top:0.5rem;font-size:0.85rem;color:var(--text);">${n.evidence || ""}</p>
            </div>
          `;
        }
      } else {
        newsHtml = `<p class="text-muted">No dated news articles or statutory registry announcements found.</p>`;
      }
      document.getElementById("newsContent").innerHTML = newsHtml;

      // Jobs Tab
      let jobsHtml = "";
      if (comp.has_explicit_zero_vacancies) {
        jobsHtml = `<div class="badge badge-info" style="display:block;padding:1rem;font-size:1rem;">✓ Verified: The company's official careers portal confirms <strong>0 active job vacancies</strong> ("ingen ledige stillinger").</div>`;
      } else if (comp.job_postings && comp.job_postings.length > 0) {
        for (const j of comp.job_postings) {
          jobsHtml += `
            <div class="card-item">
              <div class="card-item-header">
                <strong><a href="${j.canonical_url || j.url || '#'}" target="_blank">${j.title}</a></strong>
                <span class="badge ${j.status === 'open' ? 'badge-success' : 'badge-light'}">${(j.status || 'OPEN').toUpperCase()}</span>
              </div>
              <small class="text-muted">Location: ${j.location || "Norway"} | Deadline: ${j.application_deadline || "Open"}</small>
            </div>
          `;
        }
      } else {
        jobsHtml = `<p class="text-muted">No open career listings published on verified corporate channels.</p>`;
      }
      document.getElementById("jobsContent").innerHTML = jobsHtml;

      // Socials Tab
      let socHtml = "";
      const socials = (comp.social_links || []).filter(s => s.is_verified || s.status === 'verified');
      if (socials.length > 0) {
        for (const s of socials) {
          socHtml += `
            <div style="margin-bottom:0.75rem;">
              <a href="${s.url}" target="_blank" class="badge badge-primary" style="font-size:0.95rem;padding:0.5rem 1rem;">
                ${s.platform.toUpperCase()} ${s.handle ? '(@' + s.handle + ')' : ''} ✓ Verified Exact Entity
              </a>
            </div>
          `;
        }
      } else {
        socHtml = `<p class="text-muted">No verified exact-entity corporate social accounts identified. (Parent, subsidiary, personal, and third-party profiles isolated).</p>`;
      }
      document.getElementById("socialsContent").innerHTML = socHtml;

      // Evidence Explorer Tab
      let evHtml = `<table><thead><tr><th>Fact Key</th><th>Category</th><th>Value</th><th>Source</th><th>Confidence</th></tr></thead><tbody>`;
      const facts = comp.facts || {};
      for (const [k, f] of Object.entries(facts)) {
        let val = typeof f.value === 'object' ? JSON.stringify(f.value) : String(f.value);
        if (val.length > 90) val = val.substring(0, 87) + "...";
        evHtml += `
          <tr>
            <td><code>${k}</code></td>
            <td><span class="badge badge-light">${f.category || ''}</span></td>
            <td>${val}</td>
            <td><a href="${f.source_url}" target="_blank">${f.source_title || f.source_type}</a></td>
            <td><span class="badge badge-success">${(f.confidence || 1.0).toFixed(2)}</span></td>
          </tr>
        `;
      }
      evHtml += `</tbody></table>`;
      document.getElementById("evidenceContent").innerHTML = evHtml;
    }

    function switchTab(tabId) {
      document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
      document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));

      event.target.classList.add("active");
      document.getElementById(`tab-${tabId}`).classList.add("active");
    }

    function exportHtmlReport() {
      if (!currentEnvelope) return;
      window.open(`/api/export/html/${currentEnvelope.organisation_number}`, "_blank");
    }

    function exportJsonEnvelope() {
      if (!currentEnvelope) return;
      const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(currentEnvelope, null, 2));
      const dl = document.createElement("a");
      dl.setAttribute("href", dataStr);
      dl.setAttribute("download", `signalpost_${currentEnvelope.organisation_number}.json`);
      dl.click();
    }

    function copySynthesisMarkdown() {
      if (!currentEnvelope || !currentEnvelope.synthesis) return;
      navigator.clipboard.writeText(currentEnvelope.synthesis);
      alert("Synthesis markdown copied to clipboard!");
    }
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Serve the interactive single-page application dashboard."""
    return INDEX_HTML


@app.get("/api/health")
def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "agent": "Signalpost", "version": "2.0.0"}


@app.get("/api/status")
def status() -> dict[str, Any]:
    """Retrieve database profiles count and operational budget consumption."""
    count = company_repository.count_profiles()
    metrics = global_budget.get_summary()
    return {
        "stored_profiles_count": count,
        "metrics": metrics,
    }


@app.get("/api/research/{orgnr}", response_model=CompanyResultEnvelope)
def research_endpoint(
    orgnr: str,
    crawl_website: bool = Query(default=True, description="Whether to discover and crawl official website"),
) -> CompanyResultEnvelope:
    """Execute end-to-end grounded research on a Norwegian company by organization number."""
    is_valid, clean_orgnr, reason = validate_orgnr(orgnr)
    if not is_valid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid Norwegian organization number '{orgnr}': {reason}",
        )

    envelope = research_company_pipeline(clean_orgnr, crawl_website=crawl_website)
    return envelope


@app.get("/api/export/html/{orgnr}", response_class=HTMLResponse)
def export_html_endpoint(orgnr: str) -> str:
    """Generate and return a standalone, publication-grade HTML report."""
    is_valid, clean_orgnr, reason = validate_orgnr(orgnr)
    if not is_valid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid Norwegian organization number '{orgnr}': {reason}",
        )

    envelope = research_company_pipeline(clean_orgnr, crawl_website=True)
    return generate_standalone_html_report(envelope)


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Launch the Signalpost Web UI server."""
    import uvicorn

    print(f"\n========================================================")
    print(f" Signalpost Web UI & Research Agent Server")
    print(f" Serving live at: http://{host}:{port}")
    print(f" Press Ctrl+C to exit.")
    print(f"========================================================\n")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
