#!/bin/bash
mvn clean test 2>&1 | tee mvn.log
mvn pitest:mutationCoverage -Dfeatures=+EXPORT 2>&1 | tee pit.log

