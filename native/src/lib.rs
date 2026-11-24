//! Experimental native temporal engine for string keys and flat i64 records.
//! No Python objects are retained in the engine; owned records prevent aliasing.
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::Arc;

pub type Record = BTreeMap<String, i64>;
type Key = String;
type Revision = u64;
const CHECKPOINT: usize = 32;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error { Missing, InvalidRevision, UnknownIndex, RevisionOverflow }

#[derive(Debug)]
enum Payload { Full(Arc<Record>), Delta(Vec<(String, Option<i64>)>), Deleted }
#[derive(Debug)]
struct Event { revision: Revision, payload: Payload }
#[derive(Debug)]
struct Interval { start: Revision, end: Option<Revision> }
type Postings = HashMap<Key, Vec<Interval>>;
type Index = BTreeMap<i64, Postings>;

/// Single-owner engine. The Python binding serializes access using a mutex.
#[derive(Default, Debug)]
pub struct Engine {
    revision: Revision,
    current: HashMap<Key, Arc<Record>>,
    history: HashMap<Key, Vec<Event>>,
    indexes: BTreeMap<String, Index>,
}

impl Engine {
    pub fn new(fields: impl IntoIterator<Item=String>) -> Self {
        Self { indexes: fields.into_iter().map(|f| (f, Index::new())).collect(), ..Self::default() }
    }
    pub fn revision(&self) -> Revision { self.revision }
    pub fn len(&self) -> usize { self.current.len() }
    pub fn is_empty(&self) -> bool { self.current.is_empty() }
    pub fn get(&self, key: &str) -> Option<Record> { self.current.get(key).map(|v| (**v).clone()) }
    fn validate(&self, revision: Revision) -> Result<(), Error> {
        if revision > self.revision { Err(Error::InvalidRevision) } else { Ok(()) }
    }
    pub fn put(&mut self, key: Key, value: Record) -> Result<Revision, Error> {
        self.commit(key, Some(Arc::new(value)))
    }
    pub fn delete(&mut self, key: &str) -> Result<Revision, Error> {
        if !self.current.contains_key(key) { return Err(Error::Missing); }
        self.commit(key.to_owned(), None)
    }
    fn commit(&mut self, key: Key, value: Option<Arc<Record>>) -> Result<Revision, Error> {
        let revision = self.revision.checked_add(1).ok_or(Error::RevisionOverflow)?;
        let old = self.current.get(&key);
        let events = self.history.entry(key.clone()).or_default();
        let payload = match (&value, old) {
            (None, _) => Payload::Deleted,
            (Some(new), Some(old)) if events.len() % CHECKPOINT != 0 => {
                let mut delta = Vec::new();
                for (field, val) in new.iter() {
                    if old.get(field) != Some(val) { delta.push((field.clone(), Some(*val))); }
                }
                for field in old.keys() {
                    if !new.contains_key(field) { delta.push((field.clone(), None)); }
                }
                if delta.len() < new.len() { Payload::Delta(delta) }
                else { Payload::Full(Arc::clone(new)) }
            }
            (Some(new), _) => Payload::Full(Arc::clone(new)),
        };
        for (field, index) in &mut self.indexes {
            let before = old.and_then(|v| v.get(field)).copied();
            let after = value.as_ref().and_then(|v| v.get(field)).copied();
            if before == after { continue; }
            if let Some(v) = before {
                let interval = index.get_mut(&v).unwrap().get_mut(&key).unwrap().last_mut().unwrap();
                interval.end = Some(revision);
            }
            if let Some(v) = after {
                index.entry(v).or_default().entry(key.clone()).or_default().push(Interval { start: revision, end: None });
            }
        }
        events.push(Event { revision, payload });
        if let Some(v) = value { self.current.insert(key, v); } else { self.current.remove(&key); }
        self.revision = revision;
        Ok(revision)
    }
    pub fn at(&self, key: &str, revision: Revision) -> Result<Option<Record>, Error> {
        self.validate(revision)?;
        let Some(events) = self.history.get(key) else { return Ok(None); };
        let end = events.partition_point(|e| e.revision <= revision);
        if end == 0 { return Ok(None); }
        let start = (0..end).rev().find(|&i| !matches!(&events[i].payload, Payload::Delta(_))).expect("history begins with checkpoint");
        let mut state = match &events[start].payload {
            Payload::Full(v) => (**v).clone(), Payload::Deleted => return Ok(None), _ => unreachable!(),
        };
        for event in &events[start+1..end] {
            if let Payload::Delta(changes) = &event.payload {
                for (field, value) in changes { if let Some(v) = value { state.insert(field.clone(), *v); } else { state.remove(field); } }
            }
        }
        Ok(Some(state))
    }
    pub fn snapshot(&self, revision: Revision) -> Result<BTreeMap<Key, Record>, Error> {
        self.validate(revision)?;
        let mut result = BTreeMap::new();
        for key in self.history.keys() { if let Some(v) = self.at(key, revision)? { result.insert(key.clone(), v); } }
        Ok(result)
    }
    pub fn rollback(&mut self, key: &str, revision: Revision) -> Result<Revision, Error> {
        match self.at(key, revision)? { Some(v) => self.put(key.to_owned(), v), None if self.current.contains_key(key) => self.delete(key), None => Ok(self.revision) }
    }
    /// Inclusive bounds; None means unbounded. Only configured fields are indexed.
    pub fn find_range(&self, field: &str, low: Option<i64>, high: Option<i64>, revision: Revision) -> Result<BTreeMap<Key, Record>, Error> {
        self.validate(revision)?;
        let index = self.indexes.get(field).ok_or(Error::UnknownIndex)?;
        if matches!((low, high), (Some(a), Some(b)) if a > b) { return Ok(BTreeMap::new()); }
        use std::ops::Bound::{Included, Unbounded};
        let bounds = (low.map_or(Unbounded, Included), high.map_or(Unbounded, Included));
        let mut keys = BTreeSet::new();
        for (_, postings) in index.range(bounds) {
            for (key, intervals) in postings {
                let pos = intervals.partition_point(|i| i.start <= revision);
                if pos > 0 && intervals[pos-1].end.map_or(true, |e| revision < e) { keys.insert(key); }
            }
        }
        let mut result = BTreeMap::new();
        for key in keys { if let Some(v) = self.at(key, revision)? { result.insert(key.clone(), v); } }
        Ok(result)
    }
    pub fn find(&self, field: &str, value: i64, revision: Revision) -> Result<BTreeMap<Key, Record>, Error> {
        self.find_range(field, Some(value), Some(value), revision)
    }
    pub fn delta_count(&self) -> usize {
        self.history.values().flatten().filter(|e| matches!(&e.payload, Payload::Delta(_))).count()
    }
}

#[cfg(feature = "python")]
mod python;
