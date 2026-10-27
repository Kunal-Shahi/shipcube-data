# shipcube-data

ShipCube's data layer is a semantic layer whose models live in a database, not in YAML files.

You define base cubes, measures, dimensions and joins through versioned **model migrations**, which are applied and stored in a catalog database. Queries are typed and compile to SQL over a single warehouse database, organised by schema and partition.

> Status: design in progress. See `docs/` once the first PR lands.

Part of [ShipCube](https://github.com/Kunal-Shahi/shipcube).

## License

Apache-2.0
