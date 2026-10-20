"""Explicit orientation-aware OCR preserves all old V5 identities by default."""
from copy import deepcopy
from pathlib import Path
import json
import pytest
from buenapro_worker.prod2 import embedding_corpus as corpus

CONFIG=json.loads((Path(__file__).resolve().parents[3]/'docs/sources/prod2-embedding-reading.runtime.v5.json').read_text())['corpus']


def test_old_v5_default_has_no_new_identity_key():
    value=corpus._config(CONFIG)
    assert 'partial_ocr_psm' not in value
    assert corpus._config(value)==value


def test_explicit_orientation_mode_is_versioned_in_source_and_ocr_cache_identity():
    old=corpus._config(CONFIG)
    expanded=corpus._config({**CONFIG,'partial_ocr_psm':'1'})
    assert expanded=={**old,'partial_ocr_psm':'1'}
    assert corpus.canonical_hash(old)!=corpus.canonical_hash(expanded)
    for bad in ('0','3','6',1,None):
        with pytest.raises(corpus.CorpusError):corpus._config({**CONFIG,'partial_ocr_psm':bad})
    strict={k:v for k,v in CONFIG.items() if k!='page_failure_policy'}
    with pytest.raises(corpus.CorpusError):corpus._config({**strict,'partial_ocr_psm':'1'})


def test_partial_one_pass_uses_only_explicit_psm_without_threshold_relaxation(monkeypatch,tmp_path):
    calls=[]
    sample={'text':'Texto verificable de las bases '*35,'acceptable':True,'reasons':[],
            'word_count':175,'mean_confidence':93,'confident_word_ratio':.96}
    def once(pdf,number,private,config,*,psm='3'):
        calls.append((psm,config['minimum_ocr_confidence'],config['minimum_ocr_confident_word_ratio']))
        return deepcopy(sample)
    monkeypatch.setattr(corpus,'_ocr_page_once',once)
    plain=corpus._ocr_page(Path('unused.pdf'),1,tmp_path,corpus._config(CONFIG))
    oriented=corpus._ocr_page(Path('unused.pdf'),1,tmp_path,corpus._config({**CONFIG,'partial_ocr_psm':'1'}))
    assert calls==[('3',65.0,.85),('1',65.0,.85)]
    assert plain['text']==oriented['text']
    assert plain['ocr_attempts'][0]['psm']=='3'
    assert oriented['ocr_attempts'][0]['psm']=='1'
