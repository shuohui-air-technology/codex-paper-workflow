#!/usr/bin/env python3
"""Fail-closed validation for final-edit and post-Humanizer audit receipts."""
from __future__ import annotations
import argparse, hashlib, json, re, zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

SHA=re.compile(r"^sha256:[0-9a-fA-F]{64}$")
CHECKS={"numbers_units","equations","citations","figures_tables","technical_terms","comparison_direction","uncertainty","causal_strength","scope_limits","author_approved_wording"}
PROTECTED_EXTRACTOR="paper-workflow-orchestrator/protected-content-v1"
PROTECTED_PATTERNS={
    "numbers_units": re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:\s*(?:%|kg|g|mg|km|m|cm|mm|s|ms|h|Hz|kHz|MHz|GB|MB|°C|K|Pa|kPa|MPa|mol|L|mL|μm|nm)\b)?",re.I),
    "equations": re.compile(r"\$[^$\n]+\$|\\\([^\n]+?\\\)|\\\[[\s\S]+?\\\]|\\(?:eqref|ref)\{[^}]+\}"),
    "citations": re.compile(r"\[(?:\d+[\s,;\-]*)+\]|\([A-Z][A-Za-z'’-]+(?:\s+et\s+al\.)?,\s*\d{4}[a-z]?\)|10\.\d{4,9}/[-._;()/:A-Z0-9]+",re.I),
    "figures_tables": re.compile(r"\b(?:Fig(?:ure)?|Table|Eq(?:uation)?)\.?\s*[A-Z]?\d+[A-Za-z]?\b|(?:图|表|公式)\s*[A-Z]?\d+[A-Za-z]?",re.I),
    "comparison_direction": re.compile(r"\b(?:increase[ds]?|decrease[ds]?|higher|lower|greater|less|improv(?:e[ds]?|ement)|worsen(?:ed|s)?)\b|(?:增加|降低|升高|减少|改善|恶化|高于|低于)"),
    "uncertainty": re.compile(r"\b(?:may|might|could|possibly|likely|unlikely|suggest(?:s|ed)?|uncertain(?:ty)?)\b|(?:可能|或许|大概|表明|不确定)"),
    "causal_strength": re.compile(r"\b(?:cause[sd]?|causal(?:ly|ity)?|lead(?:s|ing)?\s+to|result(?:s|ed)?\s+in|due\s+to|determin(?:e[sd]?|istic))\b|(?:导致|引起|造成|由于|决定|因果)"),
    "scope_limits": re.compile(r"\b(?:only|except|limited\s+to|restricted\s+to|within|under|among|at\s+most|at\s+least)\b|(?:仅|只|除外|限于|范围内|至多|至少)"),
    "negation": re.compile(r"\b(?:not|no|never|without|cannot|can't|neither|nor)\b|(?:不|未|无|没有|不能|并非|无法)"),
}

def digest(path:Path)->str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1048576),b""): h.update(block)
    return "sha256:"+h.hexdigest()

def normalized_sha(value:Any)->str|None:
    if not isinstance(value,str): return None
    raw=value.strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}",raw): raw="sha256:"+raw
    return raw if SHA.fullmatch(raw) else None

def canonical_json_sha(value:Any)->str:
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf-8")
    return "sha256:"+hashlib.sha256(raw).hexdigest()

def docx_text_and_parts(path:Path)->tuple[str,list[str],list[str]]:
    texts:list[str]=[]; parts:list[str]=[]; revisions:list[str]=[]
    with zipfile.ZipFile(path) as archive:
        names=sorted(name for name in archive.namelist() if name.startswith("word/") and name.endswith(".xml"))
        for name in names:
            root=ET.fromstring(archive.read(name)); part_text="".join(root.itertext())
            if part_text: texts.append(part_text); parts.append(name)
            for elem in root.iter():
                local=elem.tag.rsplit("}",1)[-1]
                if local in {"ins","del","moveFrom","moveTo","trackRevisions"}: revisions.append(f"{name}:{local}")
    if "word/document.xml" not in parts: raise ValueError("DOCX has no readable word/document.xml")
    return "\n".join(texts),parts,sorted(revisions)

def manuscript_text(path:Path)->tuple[str,dict[str,list[str]]]:
    suffix=path.suffix.lower()
    if suffix==".docx":
        text,parts,revisions=docx_text_and_parts(path)
        return text,{"docx_parts":parts,"docx_revision_markers":revisions}
    if suffix in {".md",".markdown",".txt",".tex"}: return path.read_text(encoding="utf-8-sig"),{}
    raise ValueError(f"unsupported protected-content format: {suffix or '[none]'}")

def protected_inventory(path:Path,literals:list[dict[str,Any]]|None=None)->dict[str,list[str]]:
    text,structural=manuscript_text(path)
    inventory={name:sorted(Counter(m.group(0).casefold() for m in pattern.finditer(text)).elements()) for name,pattern in PROTECTED_PATTERNS.items()}
    literal_values:list[str]=[]
    for item in literals or []:
        if not isinstance(item,dict) or item.get("kind") not in {"technical_term","author_approved_wording"} or not isinstance(item.get("text"),str) or not item["text"]:
            raise ValueError("protected literal is invalid")
        needle=item["text"]
        literal_values.extend([f"{item['kind']}:{needle.casefold()}"]*text.casefold().count(needle.casefold()))
    inventory["declared_literals"]=sorted(literal_values)
    inventory.update(structural)
    return inventory

def normalized_section(value:str)->str:
    return re.sub(r"\s+"," ",re.sub(r"^\d+(?:\.\d+)*[.)]?\s*","",value.strip().casefold())).strip() or "all"

def markdown_sections(path:Path)->dict[str,str]:
    sections:dict[str,list[str]]={"preamble":[]}; current="preamble"; fenced=False; structure:list[str]=[]
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if re.match(r"^\s*(```|~~~)",line): fenced=not fenced
        match=None if fenced else re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$",line)
        if match:
            current=normalized_section(match.group(1)); sections.setdefault(current,[])
            level=len(line)-len(line.lstrip('#'))
            sections[current].append(f"@@heading-level:{level}"); structure.append(f"{level}:{current}")
        else: sections.setdefault(current,[]).append(line.rstrip())
    result={name:"\n".join(lines).strip() for name,lines in sections.items()}; result["__document_structure__"]="\n".join(structure)
    return result

def docx_sections(path:Path)->dict[str,str]:
    ns="{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    sections:dict[str,list[str]]={"preamble":[]}; current="preamble"; structure:list[str]=[]
    with zipfile.ZipFile(path) as archive:
        root=ET.fromstring(archive.read("word/document.xml"))
        for paragraph in root.iter(ns+"p"):
            text="".join(node.text or "" for node in paragraph.iter(ns+"t"))
            style=paragraph.find("./"+ns+"pPr/"+ns+"pStyle")
            style_name="" if style is None else style.attrib.get(ns+"val","")
            if text.strip() and re.match(r"^(?:Heading|标题)\s*[1-9]",style_name,re.I):
                current=normalized_section(text); sections.setdefault(current,[])
                sections[current].append(f"@@heading-style:{style_name.casefold()}"); structure.append(f"{style_name.casefold()}:{current}")
            else:
                paragraph_hash=hashlib.sha256(ET.tostring(paragraph,encoding="utf-8")).hexdigest()
                sections.setdefault(current,[]).append(text+f"\n@@paragraph-xml:{paragraph_hash}")
    result={name:"\n".join(lines).strip() for name,lines in sections.items()}; result["__document_structure__"]="\n".join(structure)
    return result

def docx_auxiliary_fingerprint(path:Path)->dict[str,str]:
    with zipfile.ZipFile(path) as archive:
        result={name:hashlib.sha256(archive.read(name)).hexdigest() for name in sorted(archive.namelist()) if name!="word/document.xml" and not name.endswith("/")}
        root=ET.fromstring(archive.read("word/document.xml"))
        ns="{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        for parent in root.iter():
            for child in list(parent):
                if child.tag==ns+"p": parent.remove(child)
        result["word/document.xml#non-paragraph-structure"]=hashlib.sha256(ET.tostring(root,encoding="utf-8")).hexdigest()
        return result

def deterministic_changed_sections(canonical:Path,candidate:Path)->list[str]:
    suffix=canonical.suffix.lower()
    if candidate.suffix.lower()!=suffix: raise ValueError("candidate format differs from canonical")
    if suffix in {".md",".markdown"}: before,after=markdown_sections(canonical),markdown_sections(candidate)
    elif suffix==".docx": before,after=docx_sections(canonical),docx_sections(candidate)
    elif suffix in {".txt",".tex"}: return [] if canonical.read_bytes()==candidate.read_bytes() else ["all"]
    else: raise ValueError(f"unsupported change-detection format: {suffix or '[none]'}")
    changed=sorted(name for name in set(before)|set(after) if before.get(name)!=after.get(name))
    if suffix==".docx":
        _,before_parts,before_revisions=docx_text_and_parts(canonical); _,after_parts,after_revisions=docx_text_and_parts(candidate)
        if before_parts!=after_parts or before_revisions!=after_revisions or docx_auxiliary_fingerprint(canonical)!=docx_auxiliary_fingerprint(candidate): changed.append("document-auxiliary")
    return sorted(set(changed))

def load(path:Path)->dict[str,Any]:
    value=json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value,dict): raise ValueError(f"JSON root must be an object: {path}")
    return value

def bound(receipt:Path,p:dict[str,Any],name:str,errors:list[str])->Path|None:
    raw,expected=p.get(name+"_path"),p.get(name+"_sha256")
    if not isinstance(raw,str) or not raw.strip(): errors.append(name+"_path must be non-empty"); return None
    if not isinstance(expected,str) or not SHA.fullmatch(expected): errors.append(name+"_sha256 is invalid"); return None
    path=Path(raw); path=path if path.is_absolute() else receipt.parent/path
    try: path=path.resolve(strict=True)
    except (OSError,RuntimeError) as exc: errors.append(f"{name}_path cannot be resolved: {exc}"); return None
    if not path.is_file(): errors.append(name+"_path is not a file"); return None
    if digest(path).casefold()!=expected.casefold(): errors.append(name+"_sha256 mismatch")
    return path

def calc_scope(canonical:str,paths:list[str],sections:list[str],mode:str)->str:
    raw=json.dumps({"canonical_sha256":canonical,"authorized_paths":paths,"authorized_sections":sections,"mode":mode},sort_keys=True,separators=(",",":")).encode()
    return "sha256:"+hashlib.sha256(raw).hexdigest()

def pass_checks(value:Any,label:str,errors:list[str])->None:
    if not isinstance(value,dict): errors.append(label+" must be an object"); return
    missing=CHECKS-set(value); failed=[k for k in CHECKS if value.get(k)!="pass"]
    if missing: errors.append(label+" missing: "+", ".join(sorted(missing)))
    if failed: errors.append(label+" not pass: "+", ".join(sorted(failed)))

def editor_metadata(text:str)->tuple[str|None,str|None,str|None]:
    if not text.startswith("---") or "\n---" not in text[3:]: return None,None,None
    front=text.split("---",2)[1]
    name_match=re.search(r"(?m)^name:\s*([^\s]+)\s*$",front)
    metadata_match=re.search(r"(?ms)^metadata:\s*\n(?P<body>(?:[ \t]+.*\n?)*)",front)
    body=metadata_match.group("body") if metadata_match else ""
    version_match=re.search(r"(?m)^\s+version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$",body)
    capability_match=re.search(r"(?m)^\s+capability_schema:\s*([^\s]+)\s*$",body)
    return (name_match.group(1) if name_match else None,version_match.group(1) if version_match else None,capability_match.group(1) if capability_match else None)

def validate(receipt:Path)->dict[str,Any]:
    errors:list[str]=[]
    try: p=load(receipt)
    except (OSError,UnicodeError,ValueError,json.JSONDecodeError) as exc: return {"status":"blocked","errors":[str(exc)]}
    mode=p.get("mode")
    for ok,msg in ((p.get("schema_version")==1,"schema_version must be 1"),(mode in {"revise","audit","learn"},"invalid mode"),(p.get("validation_status")=="pass","validation_status must be pass"),(p.get("validity_status")=="clear","validity_status must be clear"),(p.get("canonical_mutation_allowed") is False,"canonical_mutation_allowed must be false")):
        if not ok: errors.append(msg)
    canonical=bound(receipt,p,"canonical",errors); scanner=bound(receipt,p,"scanner_report",errors); stage=bound(receipt,p,"stage_receipt",errors); integrity=bound(receipt,p,"integrity_report",errors); auth=bound(receipt,p,"edit_authorization",errors); editor=bound(receipt,p,"editor_skill",errors)
    canonical_sha=p.get("canonical_sha256")
    if canonical and digest(canonical)!=canonical_sha: errors.append("canonical changed during validation")
    paths,sections=p.get("authorized_paths"),p.get("authorized_sections")
    if not isinstance(paths,list) or not paths or not all(isinstance(x,str) and x for x in paths): errors.append("authorized_paths must be non-empty")
    if not isinstance(sections,list) or not sections or not all(isinstance(x,str) and x for x in sections): errors.append("authorized_sections must be non-empty")
    expected_scope=calc_scope(canonical_sha,paths or [],sections or [],mode) if isinstance(canonical_sha,str) else ""
    if p.get("scope_hash")!=expected_scope: errors.append("scope_hash mismatch")
    if p.get("apply_decision")!="pending": errors.append("apply_decision must remain pending until validator pass")
    if p.get("citation_numbering_policy") not in {"preserve","manager-controlled","authorized-renumber"}: errors.append("invalid citation_numbering_policy")
    if p.get("bilingual_parity_status") not in {"pass","not_applicable"}: errors.append("invalid bilingual_parity_status")
    if not isinstance(p.get("style_evidence_refs"),list) or not p["style_evidence_refs"]: errors.append("style_evidence_refs must be non-empty")
    pass_checks(p.get("protected_checks"),"protected_checks",errors)
    if p.get("editor_capability_schema")!="final-editor-v1": errors.append("unsupported editor_capability_schema")
    if editor:
        name,version,capability=editor_metadata(editor.read_text(encoding="utf-8-sig"))
        if name!="academic-manuscript-final-editor" or capability!="final-editor-v1": errors.append("editor frontmatter lacks final-editor-v1 capability")
        try:
            if tuple(map(int,(version or "0.0.0").split("."))) < (2,1,0): errors.append("editor version is below 2.1.0")
        except ValueError: errors.append("editor version is invalid")
        if p.get("editor_version")!=version: errors.append("editor_version does not match frontmatter")
    if integrity:
        q=load(integrity)
        if q.get("status")!="pass" or q.get("validity_status")!="clear" or q.get("canonical_sha256")!=canonical_sha: errors.append("integrity report is not bound and clear")
    if auth:
        q=load(auth)
        for key,value in (("status","confirmed"),("approved_by","user"),("canonical_sha256",canonical_sha),("scope_hash",expected_scope),("mode",mode)):
            if q.get(key)!=value: errors.append("edit authorization mismatch: "+key)
        try:
            if datetime.fromisoformat(q.get("expires_at","").replace("Z","+00:00"))<=datetime.now(timezone.utc): errors.append("edit authorization expired")
        except (ValueError,TypeError): errors.append("edit authorization expires_at is invalid")
    if stage:
        q=load(stage)
        for key,value in (("status","confirmed"),("approved_by","orchestrator"),("validity_status","clear"),("canonical_sha256",canonical_sha),("scope_hash",expected_scope),("integrity_report_sha256",p.get("integrity_report_sha256")),("editor_skill_sha256",p.get("editor_skill_sha256")),("editor_capability_schema","final-editor-v1"),("editor_version",p.get("editor_version"))):
            if q.get(key)!=value: errors.append("stage receipt mismatch: "+key)
    if scanner:
        q=load(scanner); files=q.get("files"); matched=False
        if isinstance(files,list) and canonical:
            matched=any(isinstance(i,dict) and i.get("path")==str(canonical) and normalized_sha(i.get("sha256_before"))==canonical_sha and normalized_sha(i.get("sha256_after"))==canonical_sha and i.get("unchanged") is True for i in files)
        if q.get("schema_version")!=1 or not q.get("scanner_version") or not matched: errors.append("scanner report is not bound to unchanged canonical")
    ledger=bound(receipt,p,"author_style_ledger",errors); dispositions=bound(receipt,p,"finding_dispositions",errors); verifier=bound(receipt,p,"protected_verifier_receipt",errors)
    if ledger:
        q=load(ledger)
        if q.get("rule_count")!=len(q.get("rules",[])) or q.get("rule_count",0)<1: errors.append("author style ledger is incomplete")
    if dispositions and scanner:
        q,s=load(dispositions),load(scanner)
        if q.get("status")!="pass" or q.get("scanner_report_sha256")!=digest(scanner) or q.get("finding_count")!=s.get("finding_count") or q.get("disposed_count")!=s.get("finding_count"): errors.append("finding dispositions are incomplete or unbound")
    if verifier:
        q=load(verifier)
        if q.get("status")!="pass" or q.get("canonical_sha256")!=canonical_sha or not q.get("verifier_id") or not q.get("evidence_refs"): errors.append("protected verifier is invalid")
        implementation=Path(str(q.get("implementation_path","")))
        implementation=implementation if implementation.is_absolute() else receipt.parent/implementation
        try: implementation=implementation.resolve(strict=True)
        except (OSError,RuntimeError): implementation=None
        if implementation!=Path(__file__).resolve() or q.get("implementation_version")!="protected-content-v1" or not implementation or q.get("implementation_sha256")!=digest(implementation): errors.append("protected verifier implementation is untrusted")
        pass_checks(q.get("protected_checks"),"verifier protected_checks",errors)
    if mode=="revise":
        candidate=bound(receipt,p,"candidate",errors); rollback=bound(receipt,p,"rollback",errors); diff=bound(receipt,p,"claim_evidence_diff",errors); changes=bound(receipt,p,"change_manifest",errors); manifest=bound(receipt,p,"protected_manifest",errors)
        if canonical and candidate and canonical==candidate: errors.append("candidate_path must differ from canonical_path")
        if canonical and rollback and canonical==rollback: errors.append("rollback_path must differ from canonical_path")
        if p.get("rollback_sha256")!=canonical_sha: errors.append("rollback_sha256 must equal canonical_sha256")
        if verifier and load(verifier).get("candidate_sha256")!=p.get("candidate_sha256"): errors.append("protected verifier candidate mismatch")
        if diff:
            q=load(diff)
            claims=q.get("claims",[])
            if q.get("status")!="pass" or q.get("canonical_sha256")!=canonical_sha or q.get("candidate_sha256")!=p.get("candidate_sha256") or q.get("claim_count")!=len(claims) or q.get("claim_count",0)<1 or q.get("claim_inventory_sha256")!=canonical_json_sha(claims): errors.append("claim/evidence diff is incomplete, unbound, or has a forged inventory hash")
            if q.get("verifier_receipt_sha256")!=p.get("protected_verifier_receipt_sha256"): errors.append("claim diff verifier hash mismatch")
            if not all(isinstance(x,dict) and x.get("status")=="pass" and x.get("evidence_refs") for x in claims): errors.append("claim ledger is invalid")
        if manifest and canonical and candidate:
            q=load(manifest); literals=q.get("literal_protections",[])
            try:
                canonical_inventory=protected_inventory(canonical,literals)
                candidate_inventory=protected_inventory(candidate,literals)
            except (OSError,UnicodeError,ValueError) as exc: errors.append("protected manifest cannot be verified: "+str(exc))
            else:
                if q.get("schema_version")!=1 or q.get("extractor_id")!=PROTECTED_EXTRACTOR or q.get("canonical_sha256")!=canonical_sha or q.get("inventory")!=canonical_inventory or q.get("inventory_sha256")!=canonical_json_sha(canonical_inventory): errors.append("protected manifest is incomplete, forged, or unbound")
                if canonical.suffix.lower() in {".md",".markdown",".txt",".tex",".docx"} and candidate_inventory!=canonical_inventory: errors.append("deterministic protected-content diff failed")
                if verifier:
                    v=load(verifier)
                    if v.get("protected_manifest_sha256")!=p.get("protected_manifest_sha256") or v.get("canonical_inventory_sha256")!=canonical_json_sha(canonical_inventory) or v.get("candidate_inventory_sha256")!=canonical_json_sha(candidate_inventory): errors.append("protected verifier execution receipt is unbound")
        if changes:
            q=load(changes); changed_paths=q.get("actual_changed_paths"); changed_sections=q.get("actual_changed_sections")
            if q.get("status")!="pass" or q.get("canonical_sha256")!=canonical_sha or q.get("candidate_sha256")!=p.get("candidate_sha256") or not q.get("verifier_id") or not q.get("evidence_refs"): errors.append("change manifest is incomplete or unbound")
            try: computed_sections=deterministic_changed_sections(canonical,candidate) if canonical and candidate else []
            except (OSError,UnicodeError,ValueError,KeyError,zipfile.BadZipFile,ET.ParseError) as exc: errors.append("actual changes cannot be verified: "+str(exc)); computed_sections=[]
            computed_paths=[canonical.name] if canonical and candidate and digest(canonical)!=digest(candidate) else []
            normalized_declared=sorted(normalized_section(x) for x in changed_sections) if isinstance(changed_sections,list) and all(isinstance(x,str) for x in changed_sections) else None
            if changed_paths!=computed_paths or normalized_declared!=computed_sections: errors.append("change manifest does not match deterministic candidate diff")
            if not isinstance(changed_paths,list) or not set(changed_paths).issubset(set(paths or [])): errors.append("candidate changes exceed authorized paths")
            authorized_normalized={normalized_section(x) for x in (sections or [])}
            if not isinstance(changed_sections,list) or ("all" not in authorized_normalized and not set(computed_sections).issubset(authorized_normalized)): errors.append("candidate changes exceed authorized sections")
    if canonical and canonical.suffix.lower()==".docx":
        for name in ("document_workflow_receipt","render_receipt"):
            extra=bound(receipt,p,name,errors)
            if extra:
                q=load(extra)
                expected_doc_sha=p.get("candidate_sha256") if mode=="revise" else canonical_sha
                if q.get("status")!="pass" or q.get("manuscript_sha256")!=expected_doc_sha: errors.append(name+" is not bound to the reviewed manuscript")
                if name=="document_workflow_receipt" and q.get("coverage_status")!="full-document-structure": errors.append("document workflow coverage is incomplete")
                if name=="render_receipt" and (not isinstance(q.get("page_count"),int) or q.get("page_count",0)<1 or q.get("all_pages_checked") is not True or not isinstance(q.get("rendered_pages"),list) or len(q.get("rendered_pages"))!=q.get("page_count")): errors.append("render receipt lacks complete page evidence")
        if scanner:
            sq=load(scanner); items=sq.get("files",[])
            if not any(isinstance(i,dict) and i.get("path")==str(canonical) and i.get("coverage_status")=="main-document-text-only" for i in items): errors.append("DOCX scanner coverage_status is invalid")
    if mode=="audit":
        humanizer=bound(receipt,p,"humanizer_receipt",errors); voice=bound(receipt,p,"author_voice_verifier",errors); drift=bound(receipt,p,"voice_drift_findings",errors); drift_dispositions=bound(receipt,p,"voice_drift_dispositions",errors)
        if p.get("author_voice_audit_status")!="pass" or p.get("voice_drift_dispositions_complete") is not True: errors.append("author voice audit is incomplete")
        if humanizer:
            q=load(humanizer)
            if q.get("status") not in {"pass","ready"} or q.get("candidate_sha256")!=canonical_sha: errors.append("Humanizer receipt is not bound to audited candidate")
        if voice:
            q=load(voice)
            if q.get("status")!="pass" or q.get("audited_sha256")!=canonical_sha or q.get("humanizer_receipt_sha256")!=p.get("humanizer_receipt_sha256") or not q.get("verifier_id") or not q.get("evidence_refs"): errors.append("author voice verifier is invalid")
        if drift and drift_dispositions:
            f,d=load(drift),load(drift_dispositions)
            findings=f.get("findings",[]); disposition_items=d.get("dispositions",[])
            finding_ids=[item.get("finding_id") for item in findings] if isinstance(findings,list) and all(isinstance(item,dict) for item in findings) else []
            disposition_ids=[item.get("finding_id") for item in disposition_items] if isinstance(disposition_items,list) and all(isinstance(item,dict) for item in disposition_items) else []
            findings_valid=bool(isinstance(findings,list) and len(finding_ids)==len(findings) and all(isinstance(x,str) and x for x in finding_ids) and len(set(finding_ids))==len(finding_ids))
            dispositions_valid=bool(isinstance(disposition_items,list) and len(disposition_ids)==len(disposition_items) and all(isinstance(x,str) and x for x in disposition_ids) and len(set(disposition_ids))==len(disposition_ids) and set(disposition_ids)==set(finding_ids) and all(item.get("decision") in {"accept","reject","defer","not_applicable"} and isinstance(item.get("evidence_refs"),list) and item.get("evidence_refs") for item in disposition_items))
            if f.get("audited_sha256")!=canonical_sha or f.get("finding_count")!=len(findings) or not findings_valid: errors.append("voice drift findings are unbound or have invalid IDs")
            if d.get("status")!="pass" or d.get("findings_sha256")!=digest(drift) or d.get("finding_count")!=len(findings) or d.get("disposed_count")!=len(disposition_items) or not dispositions_valid: errors.append("voice drift dispositions are incomplete or not one-to-one")
    return {"status":"pass" if not errors else "blocked","receipt":str(receipt),"mode":mode,"errors":errors}

def main(argv:list[str]|None=None)->int:
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument("--receipt",type=Path,required=True)
    try: result=validate(ap.parse_args(argv).receipt.resolve(strict=True))
    except (OSError,RuntimeError,UnicodeError,ValueError,json.JSONDecodeError) as exc: result={"status":"blocked","errors":[str(exc)]}
    print(json.dumps(result,ensure_ascii=False,indent=2)); return 0 if result["status"]=="pass" else 1
if __name__=="__main__": raise SystemExit(main())
