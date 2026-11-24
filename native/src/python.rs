//! PyO3 boundary for the experimental native engine.
use super::{Engine, Error, Record};
use pyo3::exceptions::{PyKeyError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::collections::BTreeMap;
use std::sync::{Mutex, MutexGuard};

fn convert(e: Error) -> PyErr {
    match e {
        Error::Missing => PyKeyError::new_err("missing key"),
        Error::InvalidRevision => PyValueError::new_err("invalid revision"),
        Error::UnknownIndex => PyKeyError::new_err("unknown index"),
        Error::RevisionOverflow => PyRuntimeError::new_err("revision overflow"),
    }
}
#[pyclass(module = "chronodict_native")]
struct NativeEngine { inner: Mutex<Engine> }
impl NativeEngine {
    fn lock(&self) -> PyResult<MutexGuard<'_, Engine>> { self.inner.lock().map_err(|_| PyRuntimeError::new_err("engine lock poisoned")) }
}
#[pymethods]
impl NativeEngine {
    #[new]
    #[pyo3(signature = (indexes=None))]
    fn new(indexes: Option<Vec<String>>) -> Self { Self { inner: Mutex::new(Engine::new(indexes.unwrap_or_default())) } }
    #[getter]
    fn revision(&self) -> PyResult<u64> { Ok(self.lock()?.revision()) }
    fn __len__(&self) -> PyResult<usize> { Ok(self.lock()?.len()) }
    fn put(&self, key: String, value: Record) -> PyResult<u64> { self.lock()?.put(key, value).map_err(convert) }
    fn delete(&self, key: &str) -> PyResult<u64> { self.lock()?.delete(key).map_err(convert) }
    #[pyo3(signature = (key, version=None))]
    fn get(&self, key: &str, version: Option<u64>) -> PyResult<Option<Record>> {
        let e = self.lock()?; match version { Some(r) => e.at(key, r).map_err(convert), None => Ok(e.get(key)) }
    }
    #[pyo3(signature = (version=None))]
    fn snapshot(&self, version: Option<u64>) -> PyResult<BTreeMap<String, Record>> {
        let e = self.lock()?; e.snapshot(version.unwrap_or(e.revision())).map_err(convert)
    }
    #[pyo3(signature = (field, value, version=None))]
    fn find(&self, field: &str, value: i64, version: Option<u64>) -> PyResult<BTreeMap<String, Record>> {
        let e = self.lock()?; e.find(field, value, version.unwrap_or(e.revision())).map_err(convert)
    }
    #[pyo3(signature = (field, low=None, high=None, version=None))]
    fn find_range(&self, field: &str, low: Option<i64>, high: Option<i64>, version: Option<u64>) -> PyResult<BTreeMap<String, Record>> {
        let e = self.lock()?; e.find_range(field, low, high, version.unwrap_or(e.revision())).map_err(convert)
    }
    fn rollback(&self, key: &str, version: u64) -> PyResult<u64> { self.lock()?.rollback(key, version).map_err(convert) }
    fn delta_count(&self) -> PyResult<usize> { Ok(self.lock()?.delta_count()) }
}
#[pymodule]
fn chronodict_native(m: &Bound<'_, PyModule>) -> PyResult<()> { m.add_class::<NativeEngine>()?; Ok(()) }
