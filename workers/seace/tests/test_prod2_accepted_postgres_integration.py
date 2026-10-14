"""Explicit opt-in PG scratch/rollback test for Jev-before-promotion decisions."""
from datetime import datetime, timezone
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from buenapro_worker.prod2 import accepted_repository as subject, repository
from buenapro_worker.prod2.analysis import JEV_MODEL, JEV_POLICY_VERSION, jev_request_hash
from buenapro_worker.prod2.discovery import validate_capture
from buenapro_worker.prod2.runtime import production_settings
from test_prod2_batch_postgres_integration import _shadow_schema

ENABLED = os.environ.get("BUENAPRO_PG_SCRATCH_TEST") == "1"


@pytest.mark.skipif(not ENABLED, reason="Explicit opt-in required for SSH/PostgreSQL scratch test")
def test_accepted_jev_claim_promotion_and_legacy_reuse_in_rolled_back_pg():
    root, schema = Path(__file__).resolve().parents[3], "prod2_accepted_scratch_"+uuid4().hex
    stage, conn = "tunnel", None
    try:
        with production_settings(ssh_host="ubuntu@168.129.177.199",
                                 ssh_key=root/"credentials/ssh-key-2026-07-14.key") as settings:
            conn=psycopg.connect(settings.database_url,row_factory=dict_row)
            stage="shadow_schema"
            public_sequences=_shadow_schema(conn,schema,root)
            conn.execute((root/"db/migrations/0038_prod2_accepted_jev_cache.sql").read_text(encoding="utf-8"))
            assert conn.execute("""SELECT count(*) AS n FROM pg_constraint WHERE conrelid=
                'prod2_jev_decision_cache'::regclass AND contype='f'""").fetchone()["n"]==0
            profile,tenant=uuid4(),uuid4()
            conn.execute("""INSERT INTO company_profiles(id,tenant_id,ruc,razon_social)
                VALUES (%s,%s,'SYNTHETIC_ACCEPTED_SCRATCH','Synthetic accepted profile')""",(profile,tenant))
            observed=datetime.now(timezone.utc)
            local=observed.astimezone(repository.LIMA)
            commercial=[{"descripcion":"Soporte software","lineas":["software"]}]
            query={"keyword":"SOFTWARE","object_type":"service","year":local.year-1,
                "published_from":f"{local.year}-01-01","published_to":local.date().isoformat(),"seace_version":"3"}
            raw_rows=[{"procedure_id":8100000000+n,"convocation_id":9100000000+n,
                "nomenclature":f"SCRATCH-{local.year}-{n}","buyer_name":"SYNTHETIC BUYER",
                "published_at":local.strftime("%d/%m/%Y %H:%M"),"description":"SOFTWARE",
                "object_label":"Servicio","reference_amount":"100.00","currency":"PEN","seace_version":"3"}
                for n in range(2)]
            rows=validate_capture({"query":query,"observed_at":observed.isoformat(),"rows":raw_rows,
                "page":{"number":1,"page_size":15,"total_results":2,"total_pages":1}}).rows
            digest=jev_request_hash("SOFTWARE",commercial_profiles=commercial,model=JEV_MODEL)
            args={"convocation_id":rows[0]["convocation_id"],"request_hash":digest,"model":JEV_MODEL,"policy_version":JEV_POLICY_VERSION}
            decision={"affinity_index":70,"model":JEV_MODEL,"policy_version":JEV_POLICY_VERSION,
                "cache_key":"prod2:jev:"+digest,"input_tokens":100,"output_tokens":20,"cost_usd":0.0000042}
            stage="durable_claim_before_any_discovery"
            claimed=subject.claim_jev(conn,**args)
            assert claimed["state"]=="claimed"
            assert conn.execute("SELECT count(*) AS n FROM prod2_discoveries").fetchone()["n"]==0
            assert subject.claim_jev(conn,**args)["state"]=="blocked"
            conn.execute("UPDATE prod2_jev_decision_cache SET lease_until=clock_timestamp()-interval '1 second' WHERE cache_key=%s",(digest,))
            assert subject.claim_jev(conn,**args)["state"]=="blocked"
            assert conn.execute("SELECT status FROM prod2_jev_decision_cache WHERE cache_key=%s",(digest,)).fetchone()["status"]=="uncertain"
            with pytest.raises(subject.AcceptedPersistenceError,match="owned"):
                subject.complete_jev(conn,request_hash=digest,claim_token=str(uuid4()),result=decision)
            completed=subject.complete_jev(conn,request_hash=digest,claim_token=claimed["claim_token"],result=decision)
            assert subject.complete_jev(conn,request_hash=digest,claim_token=claimed["claim_token"],result=decision)["cached"]
            stage="threshold_promotion_cross_convocation_cache_and_no_double_cost"
            promote_result={**completed["result"],"profile_id":str(profile),"request_context":{
                "description":"SOFTWARE","commercial_profiles":commercial}}
            run="scratch-selected-"+uuid4().hex
            first=subject.promote_accepted(conn,run_id=run,row=rows[0],query_manifest=query,observed_at=observed,jev_result=promote_result)
            replay=subject.promote_accepted(conn,run_id=run,row=rows[0],query_manifest=query,observed_at=observed,jev_result=promote_result)
            assert first["opportunity_id"]==replay["opportunity_id"] and not replay["created"]
            assert subject.claim_jev(conn,**{**args,"convocation_id":rows[1]["convocation_id"]})["state"]=="cached"
            subject.promote_accepted(conn,run_id=run,row=rows[1],query_manifest=query,observed_at=observed,jev_result=promote_result)
            assert conn.execute("SELECT count(*) AS n FROM prod2_discoveries").fetchone()["n"]==2
            assert conn.execute("SELECT count(*) AS n FROM prod2_jev_decision_cache").fetchone()["n"]==1
            assert conn.execute("SELECT coalesce(sum(cost_usd),0) AS n FROM prod2_evaluations").fetchone()["n"]==0
            assert conn.execute("SELECT count(*) AS n FROM prod2_discovery_pages").fetchone()["n"]==0
            assert conn.execute("SELECT count(*) AS n FROM prod2_details").fetchone()["n"]==0
            stage="rejection_without_canonical_or_ficha_data"
            reject_hash=jev_request_hash("REACTIVOS",commercial_profiles=commercial,model=JEV_MODEL)
            reject_claim=subject.claim_jev(conn,**{**args,"convocation_id":9100000010,"request_hash":reject_hash})
            reject_result={**decision,"affinity_index":20,"cache_key":"prod2:jev:"+reject_hash}
            rejected=subject.complete_jev(conn,request_hash=reject_hash,claim_token=reject_claim["claim_token"],result=reject_result)["result"]
            subject.record_run_member(conn,run_id=run,convocation_id=9100000010,request_hash=reject_hash,accepted=False,result=rejected)
            changed={**rows[0],"procedure_id":8100000010,"convocation_id":9100000010,"description":"REACTIVOS"}
            with pytest.raises(subject.AcceptedPersistenceError,match="below"):
                subject.promote_accepted(conn,run_id=run,row=changed,query_manifest=query,observed_at=observed,
                    jev_result={**rejected,"request_context":{"description":"REACTIVOS","commercial_profiles":commercial}})
            assert conn.execute("SELECT count(*) AS n FROM prod2_discoveries").fetchone()["n"]==2
            rejected_member=conn.execute("SELECT * FROM prod2_accepted_run_members WHERE run_id=%s AND NOT accepted",(run,)).fetchone()
            assert rejected_member["row_manifest"] is None and rejected_member["opportunity_id"] is None
            stage="legacy_done_reuse_and_unknown_billing_block"
            legacy_hash=jev_request_hash("LEGACY SOFTWARE",commercial_profiles=commercial,model=JEV_MODEL)
            legacy_args={"kind":"jev_affinity","request_hash":legacy_hash,"model":JEV_MODEL,
                "policy_version":JEV_POLICY_VERSION,"profile_id":str(profile)}
            legacy=repository.claim_evaluation(conn,rows[0]["convocation_id"],**legacy_args)
            legacy_result={**decision,"cache_key":"prod2:jev:"+legacy_hash}
            repository.complete_evaluation(conn,legacy["id"],legacy["claim_token"],legacy_result,input_tokens=100,cost_usd="0.0000042")
            reused=subject.claim_jev(conn,**{**args,"convocation_id":9100000020,"request_hash":legacy_hash})
            assert reused["state"]=="cached" and reused["reused_evaluation_id"]==legacy["id"]
            uncertain_hash=jev_request_hash("UNCERTAIN SOFTWARE",commercial_profiles=commercial,model=JEV_MODEL)
            old=repository.claim_evaluation(conn,rows[0]["convocation_id"],**{**legacy_args,"request_hash":uncertain_hash})
            conn.execute("UPDATE prod2_evaluations SET status='uncertain' WHERE id=%s",(old["id"],))
            assert subject.claim_jev(conn,**{**args,"convocation_id":9100000030,"request_hash":uncertain_hash})["state"]=="blocked"
            assert conn.execute("SELECT count(*) AS n FROM prod2_accepted_run_members WHERE run_id=%s",(run,)).fetchone()["n"]==3
            stage="public_sequence_invariance_and_rollback"
            for name,before in public_sequences.items():
                assert conn.execute(sql.SQL("SELECT last_value,is_called FROM {}").format(sql.SQL(name))).fetchone()==before
            conn.rollback()
            assert conn.execute("SELECT to_regnamespace(%s) AS ns",(schema,)).fetchone()["ns"] is None
            conn.rollback()
    except Exception as exc:
        pytest.fail(f"Accepted PG scratch failed at {stage}: {type(exc).__name__}; SQLSTATE={getattr(exc,'sqlstate',None)}",pytrace=False)
    finally:
        if conn is not None:
            conn.rollback()
            conn.close()
