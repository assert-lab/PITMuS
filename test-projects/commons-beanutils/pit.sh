#!/bin/bash
mvn clean test 2>&1 | tee mvn.log
mvn pitest:mutationCoverage 2>&1 | tee pit.log
