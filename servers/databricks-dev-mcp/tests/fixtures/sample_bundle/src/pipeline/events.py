from pyspark import pipelines as dp

@dp.materialized_view
def events():
    return spark.range(3)
