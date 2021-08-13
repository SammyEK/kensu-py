#!/usr/bin/env python
# -*- encoding: utf-8 -*-
#
# Copyright 2021 Kensu Inc
#
import logging
import traceback

from kensu.google.cloud.bigquery.job.bigquery_stats import compute_bigquery_stats
from kensu.pandas import DataFrame
from kensu.utils.dsl.extractors.external_lineage_dtos import KensuDatasourceAndSchema, GenericComputedInMemDs, \
    ExtDependencyEntry
from kensu.utils.kensu import Kensu
from kensu.utils.kensu_provider import KensuProvider
import google.cloud.bigquery as bq
import google.cloud.bigquery.job as bqj
from google.cloud.bigquery import Table
import sqlparse


class BqCommonHelpers:
    @staticmethod
    def table_to_kensu(table: Table):
        kensu = KensuProvider().instance()
        # note: there should be no report here!
        ds = kensu.extractors.extract_data_source(table, kensu.default_physical_location_ref,
                                                  logical_naming=kensu.logical_naming)
        sc = kensu.extractors.extract_schema(ds, table)
        return ds, sc


class BqOfflineParser:

    # FIXME: or should we better simply fetch schema ALL visible tables and databases !!!!???
    @staticmethod
    def get_referenced_tables_metadata(
            kensu: Kensu,
            client: bq.Client,
            query: str):
        table_infos = BqOfflineParser.get_table_infos_from_sql(client, query)
        # for table, ds, sc in table_infos:
        #     # FIXME: this possibly don't fit here well...
        #     kensu.real_schema_df[sc.to_guid()] = table

        table_id_to_bqtable = {}
        metadata = {"tables": []}
        for table, ds, sc in table_infos:
            table_id = "`" + table.full_table_id.replace(":", ".") + "`"  # FIXME: replace this in DS extractor too!
            table_md = {
                "id": table_id,
                "schema": {
                    "fields": [{"name": f.name, "type": f.field_type} for f in sc.pk.fields]
                }
            }
            table_id_to_bqtable[table_id] = table
            metadata["tables"].append(table_md)
        return metadata,  table_id_to_bqtable, table_infos

    @staticmethod
    def get_table_info_for_id(client: bq.Client, id: sqlparse.sql.Identifier):
        try:
            name = (id.get_real_name()).strip('`')
            table = client.get_table(name)
            ds, sc = BqCommonHelpers.table_to_kensu(table)  # FIXME?
            return table, ds, sc
        except:
            # FIXME this is because the current find_sql_identifiers also returns the column names...
            #  (see aboveREF_GET_TABLE)
            #  Therefore get_table of a column name should fail
            return None

    @staticmethod
    def get_table_infos_from_sql(client: bq.Client, query: str):
        sq = sqlparse.parse(query)
        ids = BqOfflineParser.find_sql_identifiers(sq[0].tokens)  # FIXME we only take the first element
        table_infos = list(
            filter(lambda x: x is not None, [BqOfflineParser.get_table_info_for_id(client, id) for id in ids]))
        return table_infos

    @staticmethod
    def find_sql_identifiers(tokens):
        for t in tokens:
            if isinstance(t, sqlparse.sql.Identifier):
                if t.is_group and len(t.tokens) > 0:
                    # String values like "World" in `N == "World"` are also Identifier
                    # but their first child is of ttype `Token.Literal.String.Symbol`
                    # although table seems to have a first child of ttype `Token.Name`
                    if str(t.tokens[0].ttype) == "Token.Name":
                        # FIXME .. this is also returning the column names... (REF_GET_TABLE)
                        yield t
            elif t.is_group:
                yield from BqOfflineParser.find_sql_identifiers(t)

    @staticmethod
    def fallback_lineage(kensu, table_infos, dest):
        global_lineage = []
        for table, ds, sc in table_infos:
            ds_path = ds.pk.location
            schema_fields = [(f.name, f.field_type) for f in sc.pk.fields]
            input = KensuDatasourceAndSchema.for_path_with_opt_schema(
                kensu,
                ds_path=ds_path,
                format='BigQuery table',
                categories=None,
                maybe_schema=schema_fields,
                f_get_stats=None  # FIXME
            )
            lin_entry = ExtDependencyEntry(
                input_ds=input,
                lineage=dict([(v.name, v.name) for v in sc.pk.fields])  # FIXME: check if output field exists
            )
            global_lineage.append(lin_entry)
        return global_lineage


class BqRemoteParser:

    @staticmethod
    def parse(kensu, client: bq.Client, query: str, db_metadata, table_id_to_bqtable) -> GenericComputedInMemDs:
        ## POST REQUEST to /lineage-and-stats-criterions
        req = {"sql": query, "metadata": db_metadata}
        url = kensu.conf.get("sql.util.url")
        logging.debug("sending request to SQL parsing service url={} request={}".format(url, str(req)))
        import requests
        lineage_resp = requests.post(url + "/lineage-and-stats-criterions", json=req)
        logging.debug("lineage_resp:" + str(lineage_resp))
        logging.debug("lineage_resp_body:" + str(lineage_resp.text))
        parsed_resp = lineage_resp.json()
        lineage_info = parsed_resp['lineage']
        stats_info = parsed_resp['stats']
        lineage = list([BqRemoteParser.convert_lineage_entry(
                lineage_entry,
                kensu=kensu,
                client=client,
                table_id_to_bqtable=table_id_to_bqtable,
                stats_info=stats_info
            ) for lineage_entry in lineage_info])
        converted_lineage = GenericComputedInMemDs(lineage=lineage)
        logging.debug('converted_lineage:' + str(converted_lineage))
        return converted_lineage

    @staticmethod
    def convert_lineage_entry(lineage_entry, kensu, client: bq.Client, table_id_to_bqtable, stats_info):
        table_id = lineage_entry['table']
        logging.debug('table_id = {}, table_id_to_bqtable.keys={}'.format(table_id, str(table_id_to_bqtable)))
        bq_table = table_id_to_bqtable.get(table_id)
        stats_values = {}
        if bq_table is not None:
            ds, sc = BqCommonHelpers.table_to_kensu(bq_table)
            ds_path = ds.pk.location
            sc = [(f.name, f.field_type) for f in sc.pk.fields]
            table_stats_info = stats_info.get(table_id, {})
            stats_aggs = table_stats_info.get('stats')
            stats_filters = table_stats_info.get('input_filters')
            bg_table_ref = bq_table.reference
            # note: making stats computation lazy in a f_get_stats lambda seem to behave very weirdly...
            # so stats are computed eagerly now
            stats_values = compute_bigquery_stats(
                table_ref=bg_table_ref,
                table=bq_table,
                client=client,
                stats_aggs=stats_aggs,
                input_filters=stats_filters)
            logging.debug(
                f'table_id {table_id} (table.ref={bg_table_ref}, ds_path: {ds_path}) got input_filters: {stats_filters} & stat_aggs:{str(stats_aggs)}')
        else:
            sc = None
            ds_path = 'bigquery:/' + table_id  # FIXME: add proper BQ prefix, and extract a shared helper
        input = KensuDatasourceAndSchema.for_path_with_opt_schema(
            kensu,
            ds_path=ds_path,
            format='BigQuery table',
            categories=None,
            maybe_schema=sc,
            f_get_stats=lambda: stats_values
        )
        return ExtDependencyEntry(
            input_ds=input,
            lineage=lineage_entry['mapping']
        )