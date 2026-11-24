//! Native correctness tests, independent of Python and PyO3.
use chronodict_native::{Engine, Error, Record};
use std::collections::BTreeMap;
fn record(score: i64) -> Record { BTreeMap::from([("score".into(), score), ("fixed".into(), 7)]) }
fn engine() -> Engine { Engine::new(["score".to_string()]) }
#[test]
fn insertion_replacement_deletion() {
    let mut e = engine(); assert!(e.is_empty());
    assert_eq!(e.put("a".into(), record(1)), Ok(1));
    assert_eq!(e.put("a".into(), record(2)), Ok(2));
    assert_eq!(e.get("a"), Some(record(2)));
    assert_eq!(e.delete("a"), Ok(3)); assert_eq!(e.get("a"), None);
    assert_eq!(e.delete("a"), Err(Error::Missing)); assert_eq!(e.revision(), 3);
}
#[test]
fn historical_intervals_and_reinsert() {
    let mut e = engine(); e.put("a".into(), record(1)).unwrap();
    e.put("a".into(), record(2)).unwrap(); e.delete("a").unwrap();
    e.put("a".into(), record(1)).unwrap();
    for (r, expected) in [(0,0),(1,1),(2,0),(3,0),(4,1)] { assert_eq!(e.find("score",1,r).unwrap().len(), expected); }
    assert_eq!(e.at("a",3), Ok(None)); assert_eq!(e.at("a",2), Ok(Some(record(2))));
}
#[test]
fn checkpoints_deltas_and_field_removal() {
    let mut e = engine();
    for n in 0..100 { e.put("a".into(),record(n)).unwrap(); }
    assert!(e.delta_count() > 90);
    for r in 1..=100 { assert_eq!(e.at("a",r),Ok(Some(record(r as i64-1)))); }
    let mut v = record(99); v.remove("score"); v.insert("other".into(), 8);
    e.put("a".into(),v.clone()).unwrap(); assert_eq!(e.at("a",101), Ok(Some(v)));
    assert!(e.find("score",99,101).unwrap().is_empty());
}
#[test]
fn range_and_validation() {
    let mut e=engine(); for n in -4..=4 {e.put(n.to_string(),record(n)).unwrap();}
    assert_eq!(e.find_range("score",Some(-2),Some(2),9).unwrap().len(),5);
    assert_eq!(e.find_range("score",None,Some(-3),9).unwrap().len(),2);
    assert!(e.find_range("score",Some(2),Some(1),9).unwrap().is_empty());
    assert_eq!(e.find("bad",0,9),Err(Error::UnknownIndex));
    assert_eq!(e.snapshot(10),Err(Error::InvalidRevision));
}
#[test]
fn rollback_and_owned_values() {
    let mut e=engine(); e.put("a".into(),record(1)).unwrap(); e.delete("a").unwrap();
    assert_eq!(e.rollback("a",1),Ok(3));
    let mut copy=e.get("a").unwrap(); copy.insert("score".into(),99);
    assert_eq!(e.get("a"),Some(record(1))); assert_eq!(e.at("a",2),Ok(None));
}
#[test]
fn deterministic_replay_oracle() {
    let mut e=engine(); let mut state=BTreeMap::new(); let mut states=vec![state.clone()];
    for i in 0..200 { let k=(i%11).to_string(); if i%5==0 && state.contains_key(&k) {e.delete(&k).unwrap();state.remove(&k);} else {let v=record((i*7)%19);e.put(k.clone(),v.clone()).unwrap();state.insert(k,v);} states.push(state.clone()); }
    for (r,s) in states.iter().enumerate() { assert_eq!(&e.snapshot(r as u64).unwrap(),s); for n in 0..19 {let expected:BTreeMap<_,_>=s.iter().filter(|(_,v)|v.get("score")==Some(&n)).map(|(k,v)|(k.clone(),v.clone())).collect();assert_eq!(e.find("score",n,r as u64).unwrap(),expected);} }
}
